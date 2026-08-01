"""Request-local OpenAI client headers for custom-provider session affinity."""

from unittest.mock import MagicMock, patch

from hermes_cli.config import build_session_affinity_key
from run_agent import AIAgent

_ROUTE = "https://llm.internal.example/v1"
_AFFINITY_HEADER = "X-Hermes-Affinity-Key"


class _StubClient:
    def __init__(self):
        self.is_closed = False

    def close(self):
        self.is_closed = True


def _make_agent(*, session_id="parent-session", enabled=True):
    with patch("run_agent.OpenAI") as mock_openai:
        mock_openai.return_value = MagicMock()
        agent = AIAgent(
            api_key="test-key",
            base_url=_ROUTE,
            provider="custom",
            requested_provider="custom:trusted-local",
            model="test-model",
            session_id=session_id,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = _StubClient()
    agent._custom_providers = [
        {
            "name": "Trusted Local",
            "provider_key": "trusted-local",
            "base_url": _ROUTE,
            "model": "test-model",
            "session_affinity": enabled,
        }
    ]
    return agent


class _Harness:
    def __init__(self, agent):
        self.agent = agent
        self.built = []
        self.closed = []
        self.patchers = []

    def __enter__(self):
        def _fake_create(kwargs, *, reason, shared):
            if not shared:
                snapshot = dict(kwargs)
                if isinstance(snapshot.get("default_headers"), dict):
                    snapshot["default_headers"] = dict(snapshot["default_headers"])
                self.built.append((snapshot, reason))
            return _StubClient()

        def _fake_close(client, *, reason, shared):
            self.closed.append((client, reason))

        self.patchers = [
            patch.object(self.agent, "_create_openai_client", side_effect=_fake_create),
            patch.object(self.agent, "_close_openai_client", side_effect=_fake_close),
        ]
        for patcher in self.patchers:
            patcher.start()
        return self

    def __exit__(self, *_exc):
        for patcher in self.patchers:
            patcher.stop()


def _built_headers(harness):
    return harness.built[-1][0].get("default_headers", {})


def test_enabled_route_adds_exact_digest_to_request_local_client_only():
    agent = _make_agent(session_id="parent-session-123")
    shared_headers = {"X-Static": "configured"}
    agent._client_kwargs["default_headers"] = shared_headers

    with _Harness(agent) as harness:
        agent._create_request_openai_client(reason="chat_completion_request")

    assert _built_headers(harness) == {
        "X-Static": "configured",
        _AFFINITY_HEADER: build_session_affinity_key(_ROUTE, "parent-session-123"),
    }
    assert shared_headers == {"X-Static": "configured"}
    assert agent._client_kwargs["default_headers"] is shared_headers
    assert _AFFINITY_HEADER not in agent._client_kwargs["default_headers"]


def test_generated_header_wins_case_insensitively_without_mutating_configured_headers():
    agent = _make_agent(session_id="real-session")
    configured = {
        "x-hermes-affinity-key": "configured-spoof",
        "X-Other": "preserved",
    }
    agent._client_kwargs["default_headers"] = configured

    with _Harness(agent) as harness:
        agent._create_request_openai_client(reason="chat_completion_stream_request")

    headers = _built_headers(harness)
    assert headers[_AFFINITY_HEADER] == build_session_affinity_key(
        _ROUTE, "real-session"
    )
    assert "x-hermes-affinity-key" not in headers
    assert headers["X-Other"] == "preserved"
    assert configured["x-hermes-affinity-key"] == "configured-spoof"


def test_copilot_vision_headers_do_not_overwrite_opted_in_affinity():
    route = "https://api.githubcopilot.com"
    agent = _make_agent(session_id="vision-session")
    agent.base_url = route
    getattr(agent, "_client_kwargs")["base_url"] = route
    getattr(agent, "_custom_providers")[0]["base_url"] = route

    with (
        patch.object(
            agent,
            "_copilot_headers_for_request",
            return_value={"X-Copilot-Vision": "true"},
        ),
        _Harness(agent) as harness,
    ):
        agent._create_request_openai_client(
            reason="vision",
            api_kwargs={
                "messages": [
                    {"content": [{"type": "image_url", "image_url": {"url": "x"}}]}
                ]
            },
        )

    assert _built_headers(harness)["X-Copilot-Vision"] == "true"
    assert _built_headers(harness)[_AFFINITY_HEADER] == build_session_affinity_key(
        route, "vision-session"
    )


def test_disabled_and_native_routes_do_not_receive_affinity_header():
    disabled = _make_agent(enabled=False)
    native = _make_agent(enabled=True)
    native.api_mode = "anthropic_messages"

    for agent in (disabled, native):
        with _Harness(agent) as harness:
            agent._create_request_openai_client(reason="test")
        assert not any(
            key.lower() == _AFFINITY_HEADER.lower() for key in _built_headers(harness)
        )


def test_streaming_and_nonstreaming_reuse_same_route_scoped_value():
    agent = _make_agent(session_id="stable-session")

    with _Harness(agent) as harness:
        first = agent._create_request_openai_client(reason="chat_completion_request")
        agent._close_request_openai_client(first, reason="request_complete")
        second = agent._create_request_openai_client(
            reason="chat_completion_stream_request"
        )

    assert second is first
    assert len(harness.built) == 1
    assert _built_headers(harness)[_AFFINITY_HEADER] == build_session_affinity_key(
        _ROUTE, "stable-session"
    )


def test_session_change_separates_request_client_cache():
    agent = _make_agent(session_id="session-one")

    with _Harness(agent) as harness:
        first = agent._create_request_openai_client(reason="request-one")
        first_key = _built_headers(harness)[_AFFINITY_HEADER]
        agent._close_request_openai_client(first, reason="request_complete")
        agent.session_id = "session-two"
        second = agent._create_request_openai_client(reason="request-two")
        second_key = _built_headers(harness)[_AFFINITY_HEADER]

    assert second is not first
    assert len(harness.built) == 2
    assert first_key != second_key
    assert any(client is first for client, _reason in harness.closed)


def test_parent_and_delegated_child_agents_get_stable_distinct_keys():
    parent = _make_agent(session_id="parent-session")
    child = _make_agent(session_id="child-session")
    values = []

    for agent in (parent, child):
        with _Harness(agent) as harness:
            agent._create_request_openai_client(reason="primary-agent-request")
        values.append(_built_headers(harness)[_AFFINITY_HEADER])

    assert values[0] != values[1]
    assert values[0] == build_session_affinity_key(_ROUTE, "parent-session")
    assert values[1] == build_session_affinity_key(_ROUTE, "child-session")


def test_route_switch_reevaluates_opt_in_and_does_not_leak_header():
    agent = _make_agent(session_id="session")

    with _Harness(agent) as harness:
        first = agent._create_request_openai_client(reason="primary")
        assert _AFFINITY_HEADER in _built_headers(harness)
        agent._close_request_openai_client(first, reason="request_complete")

        agent.provider = "custom"
        agent.requested_provider = "custom:untrusted-fallback"
        agent.base_url = "https://fallback.example/v1"
        agent._custom_providers.append({
            "name": "Untrusted Fallback",
            "provider_key": "untrusted-fallback",
            "base_url": "https://fallback.example/v1",
            "model": "fallback-model",
            "session_affinity": False,
        })
        agent.model = "fallback-model"
        agent._client_kwargs = {
            "api_key": "fallback-key",
            "base_url": "https://fallback.example/v1",
        }
        second = agent._create_request_openai_client(reason="fallback")

    assert second is not first
    assert not any(
        key.lower() == _AFFINITY_HEADER.lower() for key in _built_headers(harness)
    )
