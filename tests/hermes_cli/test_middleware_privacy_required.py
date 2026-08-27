"""Fail-closed contract for installations that require LLM middleware."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli.middleware import (
    RequiredMiddlewareError,
    apply_llm_request_middleware,
    apply_tool_request_middleware,
    run_llm_execution_middleware,
    run_tool_execution_middleware,
)
from agent.certification_runtime import run_llm_execution


def _manager(callbacks, kind="llm_execution"):
    return SimpleNamespace(_middleware={kind: callbacks})


def test_required_execution_blocks_when_no_middleware_is_registered(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager", lambda: _manager([])
    )
    provider_calls = []

    with pytest.raises(RequiredMiddlewareError, match="required.*not registered"):
        run_llm_execution_middleware(
            {"messages": [{"role": "user", "content": "synthetic"}]},
            lambda request: provider_calls.append(request),
            required=True,
        )

    assert provider_calls == []


def test_required_execution_blocks_when_middleware_raises_before_provider(monkeypatch):
    def broken(**_kwargs):
        raise RuntimeError("privacy plugin crashed")

    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager", lambda: _manager([broken])
    )
    provider_calls = []

    with pytest.raises(RequiredMiddlewareError, match="failed before provider"):
        run_llm_execution_middleware(
            {"messages": [{"role": "user", "content": "synthetic"}]},
            lambda request: provider_calls.append(request),
            required=True,
        )

    assert provider_calls == []


def test_required_execution_allows_registered_rewrite(monkeypatch):
    def privacy_middleware(request, next_call, **_kwargs):
        return next_call({**request, "redacted": True})

    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager",
        lambda: _manager([privacy_middleware]),
    )

    result = run_llm_execution_middleware(
        {"messages": []},
        lambda request: request,
        required=True,
    )

    assert result["redacted"] is True


def test_certification_requires_execution_middleware_before_provider(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager", lambda: _manager([])
    )
    provider_calls = []
    agent = SimpleNamespace(_certification_persistence_deferred=True)

    with pytest.raises(RequiredMiddlewareError, match="required.*not registered"):
        run_llm_execution(
            agent,
            {"messages": [{"role": "user", "content": "private"}]},
            lambda request: provider_calls.append(request),
        )

    assert provider_calls == []


def test_required_request_middleware_failure_is_not_ignored(monkeypatch):
    def broken(**_kwargs):
        raise RuntimeError("request privacy plugin crashed")

    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager",
        lambda: _manager([broken], kind="llm_request"),
    )

    with pytest.raises(RequiredMiddlewareError, match="llm_request.*failed"):
        apply_llm_request_middleware(
            {"messages": [{"role": "user", "content": "private"}]},
            required=True,
        )


def test_required_request_blocks_when_no_middleware_is_registered(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager",
        lambda: _manager([], kind="llm_request"),
    )

    with pytest.raises(RequiredMiddlewareError, match="llm_request.*not registered"):
        apply_llm_request_middleware(
            {"messages": [{"role": "user", "content": "private"}]},
            required=True,
        )


def test_required_tool_request_failure_blocks_before_tool_dispatch(monkeypatch):
    def broken(**_kwargs):
        raise RuntimeError("tool request policy crashed")

    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager",
        lambda: _manager([broken], kind="tool_request"),
    )

    with pytest.raises(RequiredMiddlewareError, match="tool_request.*failed"):
        apply_tool_request_middleware(
            "write_file",
            {"path": "private"},
            required=True,
        )


def test_required_tool_request_blocks_when_none_is_registered(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager",
        lambda: _manager([], kind="tool_request"),
    )

    with pytest.raises(RequiredMiddlewareError, match="tool_request.*not registered"):
        apply_tool_request_middleware(
            "write_file",
            {"path": "private"},
            required=True,
        )


def test_required_tool_middleware_failure_blocks_tool_execution(monkeypatch):
    def broken(**_kwargs):
        raise RuntimeError("tool policy plugin crashed")

    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager",
        lambda: _manager([broken], kind="tool_execution"),
    )
    tool_calls = []

    with pytest.raises(RequiredMiddlewareError, match="tool_execution.*failed"):
        run_tool_execution_middleware(
            "write_file",
            {"path": "private"},
            lambda args: tool_calls.append(args),
            required=True,
        )

    assert tool_calls == []


@pytest.mark.parametrize("kind", ["llm_execution", "tool_execution"])
def test_required_middleware_cannot_hide_error_after_downstream_succeeds(
    monkeypatch, kind
):
    def broken_after_next(request=None, args=None, next_call=None, **_kwargs):
        payload = request if request is not None else args
        assert next_call is not None
        next_call(payload)
        raise RuntimeError("required middleware failed after execution")

    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager",
        lambda: _manager([broken_after_next], kind=kind),
    )
    downstream_calls = []

    with pytest.raises(RequiredMiddlewareError, match=rf"required.*{kind}.*failed"):
        if kind == "llm_execution":
            run_llm_execution_middleware(
                {"messages": []},
                lambda request: downstream_calls.append(request) or "provider result",
                required=True,
            )
        else:
            run_tool_execution_middleware(
                "write_file",
                {"path": "private"},
                lambda args: downstream_calls.append(args) or "tool result",
                required=True,
            )

    assert len(downstream_calls) == 1