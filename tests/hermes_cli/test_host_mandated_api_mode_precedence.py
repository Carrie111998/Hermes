"""A host-mandated wire protocol must outrank any persisted ``model.api_mode``.

Some endpoints accept exactly ONE wire protocol — DeepSeek's ``/anthropic``
route and Kimi's ``/coding`` route both speak Anthropic Messages and nothing
else. ``host_mandated_api_mode()`` exists to say so.

A persisted ``model.api_mode`` in config.yaml is just the mode of whatever
provider the user last used. When it survives a switch to one of these
endpoints it must not redirect the request onto a protocol the endpoint
cannot serve. ``model_switch.switch_model()`` already encodes this precedence;
these tests pin the same ordering into runtime resolution, which is the path
taken when the CLI re-resolves credentials with explicit base_url/api_key.

Regression guard — the failure mode was silent: resolution returned
``codex_responses`` for ``https://api.deepseek.com/anthropic``, so the agent
was built on the Responses transport and every request 404'd or 400'd against
an endpoint that only speaks Messages.
"""

from __future__ import annotations

import pytest

# Endpoints that accept exactly one wire protocol.
HOST_MANDATED_ENDPOINTS = [
    ("deepseek", "https://api.deepseek.com/anthropic"),
    ("kimi-coding", "https://api.kimi.com/coding"),
    ("anthropic", "https://api.anthropic.com"),
]

# Modes a previous provider could plausibly have left in config.yaml.
STALE_MODES = ["codex_responses", "chat_completions", "anthropic_messages"]

# The provider recorded alongside that stale mode (all foreign to the targets).
FOREIGN_PROVIDERS = ["openai", "openrouter", "copilot", "ollama", None]


@pytest.fixture(autouse=True)
def _dummy_credentials(monkeypatch):
    """Resolution needs *a* key; the value is irrelevant to wire selection."""
    for var in (
        "DEEPSEEK_API_KEY", "KIMI_API_KEY", "MOONSHOT_API_KEY",
        "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(var, "sk-test-dummy")


def _resolve(monkeypatch, provider, base_url, cfg_provider, cfg_mode):
    from hermes_cli import runtime_provider as rp

    model_cfg = {"default": "some-model"}
    if cfg_provider:
        model_cfg["provider"] = cfg_provider
    if cfg_mode:
        model_cfg["api_mode"] = cfg_mode
    monkeypatch.setattr(rp, "load_config", lambda *a, **k: {"model": dict(model_cfg)})

    return rp.resolve_runtime_provider(
        requested=provider,
        explicit_base_url=base_url,
        explicit_api_key="sk-test-dummy",
        target_model="some-model",
    )


@pytest.mark.parametrize(("provider", "base_url"), HOST_MANDATED_ENDPOINTS)
@pytest.mark.parametrize("cfg_mode", STALE_MODES)
@pytest.mark.parametrize("cfg_provider", FOREIGN_PROVIDERS)
def test_host_mandated_mode_beats_persisted_api_mode(
    monkeypatch, provider, base_url, cfg_mode, cfg_provider
):
    from hermes_cli.providers import host_mandated_api_mode

    mandated = host_mandated_api_mode(base_url)
    assert mandated is not None, f"test target is not host-mandated: {base_url}"

    try:
        runtime = _resolve(monkeypatch, provider, base_url, cfg_provider, cfg_mode)
    except Exception as exc:                      # pragma: no cover
        if "Unknown provider" in str(exc):
            pytest.skip(f"provider {provider!r} not registered in this build")
        raise

    assert runtime["api_mode"] == mandated, (
        f"{base_url} only speaks {mandated}, but a persisted "
        f"api_mode={cfg_mode!r} (provider={cfg_provider!r}) redirected it to "
        f"{runtime['api_mode']!r}"
    )


def test_absent_config_still_resolves_mandated_mode(monkeypatch):
    """Sanity anchor: with no persisted mode the endpoint already resolved right.

    Keeps the parametrized cases above honest — they must be proving that the
    stale mode is *ignored*, not that resolution happens to be broken for
    everything.
    """
    runtime = _resolve(
        monkeypatch, "deepseek", "https://api.deepseek.com/anthropic", "openai", None
    )
    assert runtime["api_mode"] == "anthropic_messages"


# ---------------------------------------------------------------------------
# The ordinary configured-provider route (no explicit credentials)
# ---------------------------------------------------------------------------
#
# ``_resolve_explicit_runtime`` is only entered when the caller passes an
# explicit api_key/base_url. A plain session — provider and base_url written
# into config.yaml by ``/model``, credentials read from the environment —
# lands on the API-key branch of ``resolve_runtime_provider`` instead. That
# branch honors a persisted ``api_mode`` whenever it belongs to the *same*
# provider, which is exactly the shape a stale mode takes after switching
# models within one provider. Both routes therefore need the same
# host-mandated-first ordering; these cases pin the second one.


def _resolve_configured(monkeypatch, provider, base_url, cfg_mode):
    """Resolve with NO explicit arguments — config + env only."""
    from hermes_cli import runtime_provider as rp

    model_cfg = {"default": "some-model", "provider": provider, "base_url": base_url}
    if cfg_mode:
        model_cfg["api_mode"] = cfg_mode
    monkeypatch.setattr(rp, "load_config", lambda *a, **k: {"model": dict(model_cfg)})

    return rp.resolve_runtime_provider(requested=provider, target_model="some-model")


@pytest.mark.parametrize(("provider", "base_url"), HOST_MANDATED_ENDPOINTS)
@pytest.mark.parametrize("cfg_mode", STALE_MODES)
def test_host_mandated_mode_beats_same_provider_persisted_mode(
    monkeypatch, provider, base_url, cfg_mode
):
    """A stale mode recorded under the *matching* provider must still lose.

    ``_provider_supports_explicit_api_mode`` deliberately accepts a persisted
    mode when the config's provider equals the runtime provider — that guard
    only screens out *foreign* providers. So a user who switched between two
    models of the same provider (a chat_completions one, then an
    Anthropic-only ``/coding`` or ``/anthropic`` one) carries the previous
    model's mode straight through it. Only the host-mandated check stops it.
    """
    from hermes_cli.providers import host_mandated_api_mode

    mandated = host_mandated_api_mode(base_url)
    assert mandated is not None, f"test target is not host-mandated: {base_url}"

    try:
        runtime = _resolve_configured(monkeypatch, provider, base_url, cfg_mode)
    except Exception as exc:                      # pragma: no cover
        if "Unknown provider" in str(exc):
            pytest.skip(f"provider {provider!r} not registered in this build")
        raise

    assert runtime["api_mode"] == mandated, (
        f"{base_url} only speaks {mandated}, but a persisted api_mode="
        f"{cfg_mode!r} recorded under its OWN provider redirected it to "
        f"{runtime['api_mode']!r}"
    )


def test_generic_endpoint_still_honors_persisted_mode(monkeypatch):
    """The new ordering must not clobber modes on non-mandated hosts.

    ``host_mandated_api_mode`` returns None for ordinary endpoints, and there
    an explicitly persisted ``api_mode`` remains authoritative — that is the
    whole point of the setting. Without this guard the fix could be
    "implemented" by always detecting from the URL.
    """
    from hermes_cli.providers import host_mandated_api_mode

    base_url = "https://api.deepseek.com/v1"
    assert host_mandated_api_mode(base_url) is None, "endpoint must be non-mandated"

    runtime = _resolve_configured(monkeypatch, "deepseek", base_url, "anthropic_messages")
    assert runtime["api_mode"] == "anthropic_messages"
