"""Tests for ``${VAR}`` secret-template expansion in the webhook adapter.

Static routes in ``config.yaml`` may reference a whole-value ``${VAR}``
template that is resolved from the process environment once, at adapter
load time. Dynamic (agent-created) routes are deliberately never expanded.
"""

import json

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.webhook import (
    WebhookAdapter,
    _resolve_secret_template,
)


def _make_adapter(routes=None, secret="", tmp_path=None, monkeypatch=None):
    if monkeypatch is not None and tmp_path is not None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    extra = {"host": "127.0.0.1", "port": 0, "routes": routes or {}}
    if secret:
        extra["secret"] = secret
    return WebhookAdapter(PlatformConfig(enabled=True, extra=extra))


# ---------------------------------------------------------------------------
# _resolve_secret_template unit behavior
# ---------------------------------------------------------------------------

def test_literal_secret_is_unchanged():
    assert _resolve_secret_template("plain-secret", "t") == "plain-secret"
    # A literal that merely CONTAINS a $ must never be rewritten.
    assert _resolve_secret_template("a$b", "t") == "a$b"
    assert _resolve_secret_template("INSECURE_NO_AUTH", "t") == "INSECURE_NO_AUTH"
    assert _resolve_secret_template("", "t") == ""
    assert _resolve_secret_template(None, "t") is None


def test_partial_template_is_not_expanded(monkeypatch):
    """Only an ENTIRE value of the form ${VAR} is a template."""
    monkeypatch.setenv("P15_TEST_VAR", "resolved")
    assert _resolve_secret_template("prefix-${P15_TEST_VAR}", "t") == "prefix-${P15_TEST_VAR}"
    assert _resolve_secret_template("${P15_TEST_VAR}-suffix", "t") == "${P15_TEST_VAR}-suffix"


def test_template_resolves_from_environment(monkeypatch):
    monkeypatch.setenv("P15_TEST_VAR", "resolved-value")
    assert _resolve_secret_template("${P15_TEST_VAR}", "t") == "resolved-value"
    assert _resolve_secret_template("  ${P15_TEST_VAR}  ", "t") == "resolved-value"


def test_template_unset_variable_raises():
    """Fail-closed: NOT os.path.expandvars, which would leave ${MISSING} in
    place and sail past the startup secret-required check (fail-OPEN)."""
    with pytest.raises(ValueError, match="unset or empty"):
        _resolve_secret_template("${P15_SURELY_UNSET_VAR}", "t")


def test_template_empty_variable_raises(monkeypatch):
    monkeypatch.setenv("P15_TEST_EMPTY", "")
    with pytest.raises(ValueError, match="unset or empty"):
        _resolve_secret_template("${P15_TEST_EMPTY}", "t")


# ---------------------------------------------------------------------------
# Adapter load-time behavior
# ---------------------------------------------------------------------------

def test_init_resolves_route_template(monkeypatch, tmp_path):
    monkeypatch.setenv("P15_ROUTE_SECRET", "s3cret")
    adapter = _make_adapter(
        routes={"r1": {"secret": "${P15_ROUTE_SECRET}", "prompt": "x"}},
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    assert adapter._static_routes["r1"]["secret"] == "s3cret"
    assert adapter._unresolved_secret_errors == []


def test_init_resolves_global_secret_template(monkeypatch, tmp_path):
    monkeypatch.setenv("P15_GLOBAL_SECRET", "gl0bal")
    adapter = _make_adapter(
        routes={"r1": {"prompt": "x"}},
        secret="${P15_GLOBAL_SECRET}",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    assert adapter._global_secret == "gl0bal"


def test_init_does_not_raise_on_unresolved_template(tmp_path, monkeypatch):
    """__init__ runs OUTSIDE the guarded startup path in gateway/run.py;
    raising there would crash-loop the whole gateway. Resolution failures
    are recorded and re-raised in connect() instead (which IS guarded)."""
    adapter = _make_adapter(
        routes={"r1": {"secret": "${P15_SURELY_UNSET_VAR}", "prompt": "x"}},
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    # The key is kept with an empty value: dropping it would make the route
    # silently inherit the global secret.
    assert "secret" in adapter._static_routes["r1"]
    assert adapter._static_routes["r1"]["secret"] == ""
    assert adapter._unresolved_secret_errors, "failure was not recorded"


@pytest.mark.asyncio
async def test_connect_raises_on_unresolved_template(tmp_path, monkeypatch):
    adapter = _make_adapter(
        routes={"r1": {"secret": "${P15_SURELY_UNSET_VAR}", "prompt": "x"}},
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    with pytest.raises(ValueError, match="P15_SURELY_UNSET_VAR"):
        await adapter.connect()


@pytest.mark.asyncio
async def test_connect_succeeds_with_resolved_template(tmp_path, monkeypatch):
    monkeypatch.setenv("P15_ROUTE_SECRET", "s3cret")
    adapter = _make_adapter(
        routes={"r1": {"secret": "${P15_ROUTE_SECRET}", "prompt": "x"}},
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    try:
        assert await adapter.connect()
    finally:
        await adapter.disconnect()


def test_config_extra_not_mutated_by_resolution(monkeypatch, tmp_path):
    """save_config round-trip: resolution must not leak the resolved value
    back into the config structure (it would be written to config.yaml)."""
    monkeypatch.setenv("P15_ROUTE_SECRET", "s3cret")
    routes = {"r1": {"secret": "${P15_ROUTE_SECRET}", "prompt": "x"}}
    adapter = _make_adapter(routes=routes, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert adapter._static_routes["r1"]["secret"] == "s3cret"
    # The original dict passed via config.extra keeps the template.
    assert routes["r1"]["secret"] == "${P15_ROUTE_SECRET}"
    assert adapter.config.extra["routes"]["r1"]["secret"] == "${P15_ROUTE_SECRET}"


# ---------------------------------------------------------------------------
# Dynamic (agent-created) routes are never expanded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dynamic_route_template_is_not_expanded(tmp_path, monkeypatch):
    """If the agent could set secret: ${SOME_VAR} on its own routes it would
    gain an oracle for comparing arbitrary gateway env vars against chosen
    signatures. Dynamic routes keep the literal value."""
    monkeypatch.setenv("P15_AGENT_VAR", "agent-secret")
    (tmp_path / "webhook_subscriptions.json").write_text(
        json.dumps({"agent-route": {"secret": "${P15_AGENT_VAR}", "prompt": "x"}}),
        encoding="utf-8",
    )
    adapter = _make_adapter(
        routes={"static": {"secret": "static-secret", "prompt": "x"}},
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    try:
        assert await adapter.connect()
        assert adapter._routes["agent-route"]["secret"] == "${P15_AGENT_VAR}"
    finally:
        await adapter.disconnect()
