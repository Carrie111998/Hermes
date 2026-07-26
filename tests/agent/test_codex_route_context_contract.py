from unittest.mock import MagicMock, patch


def test_codex_live_probe_uses_route_window_not_raw_model_maximum():
    """Codex OAuth must budget against the route-enforced context_window."""
    import agent.model_metadata as mm

    mm._codex_oauth_context_cache = {}
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "models": [
            {
                "slug": "gpt-5.4",
                "context_window": 272_000,
                "max_context_window": 1_000_000,
            }
        ]
    }

    with patch("agent.model_metadata.requests.get", return_value=fake_response), \
         patch("agent.model_metadata.save_context_length"):
        resolved = mm.get_model_context_length(
            model="gpt-5.4",
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="fake-token",
            provider="openai-codex",
        )

    assert resolved == 272_000
