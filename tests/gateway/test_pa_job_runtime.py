from __future__ import annotations

from agent.pa_constitution import load_constitution_data, resolve_context
import gateway.run as gateway_run


def _constitution():
    return load_constitution_data(
        {
            "id": "bobby_tgg",
            "agent_name": "Christopher",
            "identity": {"role": "PA"},
            "client": {"name": "TGG"},
            "job_briefs": {
                "tgg_ops_ingest": {
                    "title": "Ops",
                    "purpose": "Ingest operations.",
                    "instructions": ["Extract facts."],
                    "runtime": {"model": "gemini-3.1-flash-lite"},
                },
                "tgg_management": {
                    "title": "Management",
                    "purpose": "Answer management.",
                    "instructions": ["Summarize facts."],
                    "runtime": {"model": "gemini-3-flash-preview"},
                },
            },
            "selectors": [
                {
                    "job_type": "tgg_ops_ingest",
                    "match": {
                        "source.platform": "whatsapp",
                        "source.chat_id": "ops-chat",
                    },
                },
                {
                    "job_type": "tgg_management",
                    "match": {
                        "source.platform": "whatsapp",
                        "source.chat_id": "management-chat",
                    },
                },
            ],
        }
    )


def _runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_model_overrides = {}
    return runner


def _patch_runtime(monkeypatch):
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "gemini",
            "api_mode": None,
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "api_key": "key",
        },
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_gateway_model",
        lambda config=None: "gemini-3-flash-preview",
    )


def test_pa_job_runtime_model_routes_from_selected_job(monkeypatch):
    _patch_runtime(monkeypatch)
    constitution = _constitution()
    pa_context = resolve_context(
        {"constitution": constitution},
        {"source": {"platform": "whatsapp", "chat_id": "ops-chat"}},
    )

    model, runtime = _runner()._resolve_session_agent_runtime(
        session_key="source:ops-chat",
        user_config={},
        pa_context=pa_context,
    )

    assert model == "gemini-3.1-flash-lite"
    assert runtime["provider"] == "gemini"


def test_session_model_override_beats_pa_job_runtime(monkeypatch):
    _patch_runtime(monkeypatch)
    runner = _runner()
    runner._session_model_overrides["source:ops-chat"] = {
        "model": "gemini-3-flash-preview",
        "provider": "gemini",
    }
    constitution = _constitution()
    pa_context = resolve_context(
        {"constitution": constitution},
        {"source": {"platform": "whatsapp", "chat_id": "ops-chat"}},
    )

    model, runtime = runner._resolve_session_agent_runtime(
        session_key="source:ops-chat",
        user_config={},
        pa_context=pa_context,
    )

    assert model == "gemini-3-flash-preview"
    assert runtime["provider"] == "gemini"
