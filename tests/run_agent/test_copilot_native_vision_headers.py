from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_copilot_agent(base_url="https://api.githubcopilot.com"):
    with patch("run_agent.OpenAI") as mock_openai:
        mock_openai.return_value = MagicMock()
        agent = AIAgent(
            api_key="gh-token",
            base_url=base_url,
            provider="copilot",
            model="gpt-5.4",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    return agent


def test_request_client_adds_copilot_vision_header_for_native_image_payload():
    agent = _make_copilot_agent()
    built_kwargs = []

    def fake_create(kwargs, *, reason, shared):
        built_kwargs.append(dict(kwargs))
        return MagicMock()

    api_kwargs = {
        "model": "gpt-5.4",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            }
        ],
    }

    agent.client = object()
    with patch.object(agent, "_is_openai_client_closed", return_value=False), patch.object(
        agent, "_create_openai_client", side_effect=fake_create
    ):
        agent._create_request_openai_client(reason="test", api_kwargs=api_kwargs)

    headers = built_kwargs[-1]["default_headers"]
    assert headers["Copilot-Vision-Request"] == "true"


def test_enterprise_agent_client_has_default_copilot_headers(monkeypatch):
    base_url = "https://copilot-api.ghe.example.com"
    monkeypatch.setenv("COPILOT_API_BASE_URL", base_url)

    agent = _make_copilot_agent(base_url)

    assert agent._client_kwargs["default_headers"]["Copilot-Integration-Id"] == "vscode-chat"


def test_enterprise_request_client_adds_copilot_vision_header(monkeypatch):
    base_url = "https://copilot-api.ghe.example.com"
    monkeypatch.setenv("COPILOT_API_BASE_URL", base_url)
    agent = _make_copilot_agent(base_url)
    built_kwargs = []

    def fake_create(kwargs, *, reason, shared):
        built_kwargs.append(dict(kwargs))
        return MagicMock()

    api_kwargs = {
        "model": "gpt-5.4",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            }
        ],
    }

    agent.client = object()
    with patch.object(agent, "_is_openai_client_closed", return_value=False), patch.object(
        agent, "_create_openai_client", side_effect=fake_create
    ):
        agent._create_request_openai_client(reason="test", api_kwargs=api_kwargs)

    assert built_kwargs[-1]["default_headers"]["Copilot-Vision-Request"] == "true"


