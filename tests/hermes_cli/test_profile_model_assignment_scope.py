import asyncio
from pathlib import Path

from hermes_cli.web_routers import profiles as profiles_router


def test_profile_model_update_declares_new_session_scope(monkeypatch, tmp_path):
    profile_dir = tmp_path / "profiles" / "chip"
    profile_dir.mkdir(parents=True)
    written = []

    monkeypatch.setattr(
        profiles_router,
        "_resolve_profile_dir",
        lambda name: profile_dir,
    )
    monkeypatch.setattr(
        profiles_router,
        "_write_profile_model",
        lambda path, provider, model: written.append((path, provider, model)),
    )

    result = asyncio.run(
        profiles_router.update_profile_model_endpoint(
            "chip",
            profiles_router.ProfileModelUpdate(
                provider="openai-codex",
                model="gpt-5.6-luna",
            ),
        )
    )

    assert written == [(profile_dir, "openai-codex", "gpt-5.6-luna")]
    assert result == {
        "ok": True,
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "applies_to": "new_sessions",
    }
