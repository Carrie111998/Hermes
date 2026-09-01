"""billing_provider_identity: routable provider identity for billing records.

Regression tests for GitHub #98739: automatic fallback to a user-defined
``providers:`` entry wrote the bare billing class ``custom`` into
``billing_provider`` (conversation_loop / codex_runtime), so the dashboard
showed "custom" instead of the configured provider name. The manual /model
switch path already propagated the configured ID.
"""

from agent.agent_runtime_helpers import billing_provider_identity


class _Agent:
    """Minimal stand-in with just the attributes the helper reads."""

    def __init__(self, provider="", requested_provider=""):
        self.provider = provider
        self.requested_provider = requested_provider


def test_builtin_provider_passthrough():
    """Built-in providers are returned unchanged, requested_provider ignored."""
    agent = _Agent(provider="zai", requested_provider="something-else")
    assert billing_provider_identity(agent) == "zai"


def test_custom_class_prefers_requested_provider():
    """Bare 'custom' with a configured identity resolves to that identity."""
    agent = _Agent(provider="custom", requested_provider="my-custom-provider")
    assert billing_provider_identity(agent) == "my-custom-provider"


def test_custom_class_with_no_identity_stays_custom():
    """Anonymous custom endpoints (OPENAI_BASE_URL) have no ID to preserve.

    The helper must not invent one — bare 'custom' is the honest record.
    """
    agent = _Agent(provider="custom", requested_provider="")
    assert billing_provider_identity(agent) == "custom"


def test_custom_class_ignores_non_routable_requested_values():
    """'custom'/'auto'/blank on requested_provider are not identities."""
    for requested in ("custom", "auto", "  ", None):
        agent = _Agent(provider="custom", requested_provider=requested)
        assert billing_provider_identity(agent) == "custom"


def test_requested_provider_case_and_whitespace_normalized():
    agent = _Agent(provider=" Custom ", requested_provider="  My-Provider ")
    assert billing_provider_identity(agent) == "My-Provider"


def test_missing_attributes_tolerated():
    """Agents without the attrs (tests, bare objects) don't crash."""

    class _Bare:
        pass

    assert billing_provider_identity(_Bare()) == ""
