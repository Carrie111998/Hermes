"""Tests for the OpenCode Zen/Go WAF User-Agent fix (#15575).

opencode.ai fronts both ``/zen/v1`` and ``/zen/go/v1`` with a WAF that returns
a bare 401 (empty body) for requests carrying the OpenAI SDK's default
User-Agent, even when the API key is valid.  Hermes must send an explicit UA
on *every* client construction path, because the WAF does not care which of
them built the request:

- initial construction (``AIAgent.__init__``)
- client rebuild after a model/base-url switch
  (``_apply_client_headers_for_base_url``)
- auxiliary and async clients (``agent.auxiliary_client``)

Zen speaks the OpenAI-compatible wire for every model it proxies, Gemini
included, so none of these paths may divert to Gemini's native client.
"""
from unittest.mock import MagicMock, patch

from agent.auxiliary_client import build_opencode_headers
from run_agent import AIAgent

ZEN_BASE = "https://opencode.ai/zen/v1"
GO_BASE = "https://opencode.ai/zen/go/v1"
OTHER_BASE = "https://api.example.com/v1"


def _make_agent(base_url, model):
    return AIAgent(
        api_key="sk-test",
        base_url=base_url,
        model=model,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )


# ---------------------------------------------------------------------------
# The shared helper
# ---------------------------------------------------------------------------

def test_helper_returns_identifiable_user_agent():
    """The helper must yield a non-empty, hermes-identifying UA."""
    headers = build_opencode_headers()
    assert set(headers) == {"User-Agent"}
    ua = headers["User-Agent"]
    assert ua.startswith("hermes-agent/")
    assert ua != "hermes-agent/"  # a version is actually interpolated


def test_helper_returns_a_fresh_dict_each_call():
    """Callers mutate the returned dict; it must not be shared state."""
    first = build_opencode_headers()
    first["X-Scratch"] = "1"
    assert "X-Scratch" not in build_opencode_headers()


# ---------------------------------------------------------------------------
# Path 1: initial client construction
# ---------------------------------------------------------------------------

@patch("run_agent.OpenAI")
def test_user_agent_injected_for_zen(mock_openai):
    """Requests to opencode.ai/zen must carry a User-Agent header."""
    mock_openai.return_value = MagicMock()
    _make_agent(ZEN_BASE, "kimi-k2.5")
    headers = mock_openai.call_args.kwargs.get("default_headers", {})
    assert headers.get("User-Agent", "").startswith("hermes-agent"), (
        f"User-Agent missing or wrong for {ZEN_BASE}: {headers!r}"
    )


@patch("run_agent.OpenAI")
def test_user_agent_injected_for_go(mock_openai):
    """Same User-Agent requirement for the opencode.ai/zen/go endpoint."""
    mock_openai.return_value = MagicMock()
    _make_agent(GO_BASE, "glm-5")
    headers = mock_openai.call_args.kwargs.get("default_headers", {})
    assert headers.get("User-Agent", "").startswith("hermes-agent"), (
        f"User-Agent missing or wrong for {GO_BASE}: {headers!r}"
    )


@patch("run_agent.OpenAI")
def test_user_agent_not_injected_for_other_hosts(mock_openai):
    """The opencode-specific User-Agent must not leak to other hosts."""
    mock_openai.return_value = MagicMock()
    _make_agent(OTHER_BASE, "some-model")
    headers = mock_openai.call_args.kwargs.get("default_headers", {})
    assert not headers.get("User-Agent", "").startswith("hermes-agent")


# ---------------------------------------------------------------------------
# Path 2: client rebuild (model / base-url switch mid-session)
# ---------------------------------------------------------------------------

@patch("run_agent.OpenAI")
def test_user_agent_survives_client_rebuild(mock_openai):
    """A rebuild onto an opencode.ai base URL must re-apply the UA.

    Regression guard: the original fix only covered __init__, so switching
    models mid-session rebuilt the client without the header and the WAF
    started 401ing again.
    """
    mock_openai.return_value = MagicMock()
    agent = _make_agent(OTHER_BASE, "some-model")

    agent._apply_client_headers_for_base_url(ZEN_BASE)

    headers = agent._client_kwargs.get("default_headers", {})
    assert headers.get("User-Agent", "").startswith("hermes-agent"), (
        f"User-Agent lost on rebuild onto {ZEN_BASE}: {headers!r}"
    )


@patch("run_agent.OpenAI")
def test_rebuild_away_from_opencode_drops_the_user_agent(mock_openai):
    """Rebuilding onto another host must not keep the opencode UA."""
    mock_openai.return_value = MagicMock()
    agent = _make_agent(ZEN_BASE, "kimi-k2.5")

    agent._apply_client_headers_for_base_url(OTHER_BASE)

    headers = agent._client_kwargs.get("default_headers") or {}
    assert not headers.get("User-Agent", "").startswith("hermes-agent")


# ---------------------------------------------------------------------------
# Zen is OpenAI-compatible for every model, Gemini included
# ---------------------------------------------------------------------------

@patch("run_agent.OpenAI")
def test_gemini_model_on_zen_stays_on_openai_compatible_client(mock_openai):
    """gemini-* on opencode.ai must NOT divert to Gemini's native client.

    ``is_native_gemini_base_url`` only recognises
    generativelanguage.googleapis.com; Zen proxies Gemini over its own
    OpenAI-compatible /v1/chat/completions route, so the native wire would
    POST generateContent payloads at an endpoint that cannot parse them.
    """
    from agent.gemini_native_adapter import GeminiNativeClient

    mock_openai.return_value = MagicMock()
    agent = _make_agent(ZEN_BASE, "gemini-3-flash")

    assert not isinstance(agent.client, GeminiNativeClient)
    assert mock_openai.called, "gemini-* on Zen should build a normal OpenAI client"


@patch("run_agent.OpenAI")
def test_gemini_model_on_zen_still_gets_the_user_agent(mock_openai):
    """The WAF applies to Gemini traffic on Zen too."""
    mock_openai.return_value = MagicMock()
    _make_agent(ZEN_BASE, "gemini-3-flash")
    headers = mock_openai.call_args.kwargs.get("default_headers", {})
    assert headers.get("User-Agent", "").startswith("hermes-agent")
