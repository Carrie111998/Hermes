from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from plugins.skyai_customer import dev_gateway
import utils


def settings(tmp_path: Path, **overrides) -> dev_gateway.CanarySettings:
    values = {"profile_home": tmp_path / "profiles" / "skyai-v2-dev"}
    values.update(overrides)
    return dev_gateway.CanarySettings(**values)


def test_validate_settings_allows_loopback_without_token(tmp_path: Path) -> None:
    dev_gateway.validate_settings(settings(tmp_path))


def test_validate_settings_blocks_public_bind_without_explicit_gate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-loopback"):
        dev_gateway.validate_settings(settings(tmp_path, host="0.0.0.0"))


def test_validate_settings_requires_token_for_public_bind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bearer token"):
        dev_gateway.validate_settings(
            settings(tmp_path, host="0.0.0.0", allow_public_bind=True)
        )


def test_validate_settings_allows_private_bind_with_explicit_gate_without_token(
    tmp_path: Path,
) -> None:
    dev_gateway.validate_settings(
        settings(tmp_path, host="10.80.0.3", allow_public_bind=True)
    )


def test_trusted_proxy_authorization_uses_transport_peer_only(
    tmp_path: Path,
) -> None:
    canary_settings = settings(
        tmp_path,
        host="10.80.0.4",
        allow_public_bind=True,
        trusted_proxy_cidr="10.80.2.0/28",
    )
    dev_gateway.validate_settings(canary_settings)

    trusted_request = SimpleNamespace(
        headers={"X-Forwarded-For": "203.0.113.9"},
        remote="10.80.2.7",
    )
    untrusted_request = SimpleNamespace(
        headers={"X-Forwarded-For": "10.80.2.7"},
        remote="10.80.3.7",
    )

    assert dev_gateway._authorize(trusted_request, canary_settings) is True
    assert dev_gateway._authorize(untrusted_request, canary_settings) is False


def test_trusted_proxy_authorization_rejects_invalid_or_missing_peer(
    tmp_path: Path,
) -> None:
    canary_settings = settings(
        tmp_path,
        host="10.80.0.4",
        allow_public_bind=True,
        trusted_proxy_cidr="10.80.2.0/28",
    )

    for remote in (None, "", "10.80.2.7 ", "not-an-ip"):
        request = SimpleNamespace(headers={}, remote=remote)
        assert dev_gateway._authorize(request, canary_settings) is False


def test_bearer_or_trusted_proxy_can_authorize_without_rewriting(
    tmp_path: Path,
) -> None:
    canary_settings = settings(
        tmp_path,
        host="10.80.0.4",
        allow_public_bind=True,
        auth_token="exact-token",
        trusted_proxy_cidr="10.80.2.0/28",
    )

    bearer_request = SimpleNamespace(
        headers={"Authorization": "Bearer exact-token"},
        remote="10.80.3.7",
    )
    trusted_request = SimpleNamespace(
        headers={"Authorization": "Bearer wrong-token"},
        remote="10.80.2.7",
    )

    assert dev_gateway._authorize(bearer_request, canary_settings) is True
    assert dev_gateway._authorize(trusted_request, canary_settings) is True


@pytest.mark.parametrize("host", ["LOCALHOST", "localhost ", " 127.0.0.1", "１２７.０.０.１"])
def test_loopback_security_gate_rejects_normalized_lookalikes(
    tmp_path: Path,
    host: str,
) -> None:
    with pytest.raises(ValueError, match="non-loopback"):
        dev_gateway.validate_settings(settings(tmp_path, host=host))


def test_validate_settings_accepts_only_exact_required_discord_thread_config(
    tmp_path: Path,
) -> None:
    dev_gateway.validate_settings(
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="exact-token",
            discord_mirror_channel_id=dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
            discord_mirror_create_threads=True,
        )
    )


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        (
            {
                "discord_mirror_enabled": 1,
            },
            "enabled must be an exact boolean",
        ),
        (
            {
                "discord_mirror_create_threads": 1,
            },
            "create_threads must be an exact boolean",
        ),
        (
            {
                "discord_mirror_enabled": True,
                "discord_mirror_bot_token": "",
                "discord_mirror_channel_id": dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
                "discord_mirror_create_threads": True,
            },
            "bot token is required",
        ),
        (
            {
                "discord_mirror_enabled": True,
                "discord_mirror_bot_token": " exact-token",
                "discord_mirror_channel_id": dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
                "discord_mirror_create_threads": True,
            },
            "must not contain whitespace",
        ),
        (
            {
                "discord_mirror_enabled": True,
                "discord_mirror_bot_token": "exact-token",
                "discord_mirror_channel_id": (
                    dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID + " "
                ),
                "discord_mirror_create_threads": True,
            },
            "must exactly equal",
        ),
        (
            {
                "discord_mirror_enabled": True,
                "discord_mirror_bot_token": "exact-token",
                "discord_mirror_channel_id": "１５１０８８８７２１６１４９０１３５８",
                "discord_mirror_create_threads": True,
            },
            "must exactly equal",
        ),
        (
            {
                "discord_mirror_enabled": True,
                "discord_mirror_bot_token": "exact-token",
                "discord_mirror_channel_id": dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
                "discord_mirror_create_threads": False,
            },
            "requires one thread",
        ),
    ],
)
def test_validate_settings_rejects_discord_config_lookalikes(
    tmp_path: Path,
    overrides: dict,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        dev_gateway.validate_settings(settings(tmp_path, **overrides))


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        (
            {"compare_prod_base_url": "https://prod.example/"},
            "must not end with",
        ),
        (
            {"compare_prod_base_url": " https://prod.example"},
            "must not contain whitespace",
        ),
        (
            {"compare_prod_path": "chatkit/message"},
            "must start with",
        ),
        (
            {"voice_backend_target": "SkyAI_v2_chatkit"},
            "must exactly equal",
        ),
        (
            {"voice_backend_target": "skyai_v2_chatkit "},
            "must exactly equal",
        ),
        (
            {"voice_v1_base_url": "https://voice.example/"},
            "must not end with",
        ),
        (
            {"voice_v1_path": "／chatkit/message"},
            "must start with",
        ),
        (
            {"discord_mirror_database_url": " postgres://db.invalid/skyai"},
            "exact postgresql URL",
        ),
    ],
)
def test_validate_settings_rejects_unrepaired_config_lookalikes(
    tmp_path: Path,
    overrides: dict,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        dev_gateway.validate_settings(settings(tmp_path, **overrides))


@pytest.mark.parametrize(("value", "expected"), [("1", True), ("0", False)])
def test_env_bool_accepts_only_exact_canonical_values(
    monkeypatch,
    value: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("SKYAI_EXACT_BOOL", value)

    assert dev_gateway._env_bool("SKYAI_EXACT_BOOL") is expected


@pytest.mark.parametrize("value", ["true", "TRUE", "yes", " 1", "1 ", "１", ""])
def test_env_bool_rejects_textual_and_lookalike_values(
    monkeypatch,
    value: str,
) -> None:
    monkeypatch.setenv("SKYAI_EXACT_BOOL", value)

    with pytest.raises(ValueError, match="exactly '1' or '0'"):
        dev_gateway._env_bool("SKYAI_EXACT_BOOL")


def test_legacy_discord_bot_token_env_is_not_a_secret_alias(monkeypatch) -> None:
    monkeypatch.setenv("SKYAI_DISCORD_MIRROR_ENABLED", "1")
    monkeypatch.setenv("SKYAI_DISCORD_MIRROR_CREATE_THREADS", "1")
    monkeypatch.setenv(
        "SKYAI_DISCORD_MIRROR_CHANNEL_ID",
        dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
    )
    monkeypatch.delenv("SKYAI_DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "legacy-token-must-be-ignored")

    with pytest.raises(ValueError, match="bot token is required"):
        dev_gateway.main(["--dev"])


def test_generic_database_url_is_not_a_discord_mirror_secret_alias(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SKYAI_DISCORD_MIRROR_ENABLED", "1")
    monkeypatch.setenv("SKYAI_DISCORD_MIRROR_CREATE_THREADS", "1")
    monkeypatch.setenv("SKYAI_DISCORD_BOT_TOKEN", "exact-token")
    monkeypatch.setenv(
        "SKYAI_DISCORD_MIRROR_CHANNEL_ID",
        dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
    )
    monkeypatch.delenv("SKYAI_DISCORD_MIRROR_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://must-not-be-used.invalid/generic",
    )

    with pytest.raises(
        ValueError,
        match="SKYAI_DISCORD_MIRROR_DATABASE_URL is required",
    ):
        dev_gateway.main(
            ["--dev", "--profile-home", str(tmp_path / "profile")]
        )


def test_main_preserves_exact_env_bytes_without_truthy_defaulting(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_create_app(canary_settings):
        captured["settings"] = canary_settings
        return object()

    monkeypatch.setattr(dev_gateway, "create_app", fake_create_app)
    monkeypatch.setattr(dev_gateway.web, "run_app", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("SKYAI_COMPARE_PROD_BASE_URL", " https://prod.invalid ")
    monkeypatch.setenv("SKYAI_COMPARE_PROD_PATH", " /exact ")
    monkeypatch.setenv("SKYAI_VOICE_BACKEND_TARGET", "")
    monkeypatch.setenv("SKYAI_VOICE_V1_BASE_URL", " https://voice.invalid ")
    monkeypatch.setenv("SKYAI_VOICE_V1_PATH", " /voice ")
    monkeypatch.setenv("SKYAI_V2_CANARY_TOKEN", " exact-token ")

    assert dev_gateway.main(
        ["--dev", "--profile-home", str(tmp_path / "profile")]
    ) == 0

    canary_settings = captured["settings"]
    assert isinstance(canary_settings, dev_gateway.CanarySettings)
    assert canary_settings.compare_prod_base_url == " https://prod.invalid "
    assert canary_settings.compare_prod_path == " /exact "
    assert canary_settings.voice_backend_target == ""
    assert canary_settings.voice_v1_base_url == " https://voice.invalid "
    assert canary_settings.voice_v1_path == " /voice "
    assert canary_settings.auth_token == " exact-token "


def test_resolve_build_commit_prefers_explicit_value(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(dev_gateway.BUILD_COMMIT_ENV, "from-env")
    (tmp_path / dev_gateway.BUILD_COMMIT_FILE).write_text("from-file\n", encoding="utf-8")

    assert dev_gateway.resolve_build_commit("explicit") == "explicit"


def test_resolve_build_commit_reads_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(dev_gateway.BUILD_COMMIT_ENV, "from-env")
    (tmp_path / dev_gateway.BUILD_COMMIT_FILE).write_text("from-file\n", encoding="utf-8")

    assert dev_gateway.resolve_build_commit() == "from-env"


def test_resolve_build_commit_reads_runtime_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(dev_gateway.BUILD_COMMIT_ENV, raising=False)
    (tmp_path / dev_gateway.BUILD_COMMIT_FILE).write_text("from-file\n", encoding="utf-8")

    assert dev_gateway.resolve_build_commit() == "from-file\n"


def test_resolve_build_commit_preserves_explicit_and_env_bytes(
    monkeypatch,
) -> None:
    monkeypatch.setenv(dev_gateway.BUILD_COMMIT_ENV, " from-env\n")

    assert dev_gateway.resolve_build_commit() == " from-env\n"
    assert dev_gateway.resolve_build_commit(" explicit\t") == " explicit\t"


def test_extract_message_preserves_canonical_authored_text_exactly() -> None:
    authored = " \tИскам ваучер за двама\n"
    payload = {
        "conversation_id": "abc",
        "history": [{"role": "assistant", "content": "Здравей"}],
        "message": authored,
    }

    assert dev_gateway.extract_message(payload) == authored
    assert dev_gateway.extract_message(payload).encode("utf-8") == authored.encode("utf-8")


def test_extract_message_does_not_infer_alias_or_history_content() -> None:
    payload = {
        "text": "text alias",
        "input": "input alias",
        "messages": [
            {"role": "assistant", "content": "Здравей"},
            {"role": "customer", "content": "Имате ли свободни слотове?"},
        ],
        "history": [{"role": "user", "content": "Последен въпрос"}],
    }

    assert dev_gateway.extract_message(payload) == ""


def test_extract_message_and_metadata_reject_wrong_types_without_repair() -> None:
    with pytest.raises(ValueError, match="message must be a string"):
        dev_gateway.extract_message({"message": 7})
    with pytest.raises(ValueError, match="metadata must be an object"):
        dev_gateway._payload_metadata({"metadata": "surface=voice"})
    with pytest.raises(ValueError, match="call_id must be a nonempty string"):
        dev_gateway.voice_call_id_from_payload({"call_id": 7})


def test_extract_history_preserves_exact_canonical_roles_and_content() -> None:
    user_content = " \tПърво\n"
    assistant_content = "\nВторо \t"
    payload = {
        "history": [
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": assistant_content},
        ]
    }

    assert dev_gateway.extract_history(payload) == [
        {"role": "user", "content": user_content},
        {"role": "assistant", "content": assistant_content},
    ]


@pytest.mark.parametrize(
    "history",
    [
        [{"role": "customer", "content": "alias role"}],
        [{"role": "USER", "content": "case alias"}],
        [{"role": "user", "text": "content alias"}],
        [{"role": "user", "content": 7}],
    ],
)
def test_extract_history_rejects_noncanonical_aliases(history: list[dict]) -> None:
    with pytest.raises(ValueError):
        dev_gateway.extract_history({"history": history})


def test_authored_text_resource_limits_reject_instead_of_truncating() -> None:
    oversized = "x" * (dev_gateway.MAX_MESSAGE_CHARS + 1)

    with pytest.raises(ValueError, match="message exceeds"):
        dev_gateway.extract_message({"message": oversized})
    with pytest.raises(ValueError, match="history content exceeds"):
        dev_gateway.extract_history(
            {"history": [{"role": "user", "content": oversized}]}
        )
    with pytest.raises(ValueError, match="history exceeds"):
        dev_gateway.extract_history(
            {
                "history": [
                    {"role": "user", "content": str(index)}
                    for index in range(dev_gateway.MAX_HISTORY_TURNS + 1)
                ]
            }
        )


def test_runtime_conversation_id_hashes_exact_input_bytes_mechanically() -> None:
    external_id = "skyai-v2-compare-20260704T203902Z-gift-calm-50-sliven-" + ("x" * 120)

    runtime_id = dev_gateway.runtime_conversation_id(external_id)

    assert runtime_id == hashlib.sha256(external_id.encode("utf-8")).hexdigest()
    assert len(runtime_id) == 64
    assert runtime_id != external_id
    assert len(
        {
            dev_gateway.runtime_conversation_id("Case"),
            dev_gateway.runtime_conversation_id("case"),
            dev_gateway.runtime_conversation_id(" Case"),
            dev_gateway.runtime_conversation_id("Сase"),
        }
    ) == 4


def test_external_conversation_id_is_preserved_byte_for_byte() -> None:
    conversation_id = " \tQA:smoke/ß/🧪\n"

    extracted = dev_gateway.conversation_id_from_payload(
        {"conversation_id": conversation_id}
    )

    assert extracted == conversation_id
    assert extracted.encode("utf-8") == conversation_id.encode("utf-8")


def test_conversation_id_aliases_are_ignored_for_chat_and_voice() -> None:
    payload = {"session_id": "session-alias", "thread_id": "thread-alias"}

    with pytest.raises(ValueError, match="conversation_id must be a nonempty string"):
        dev_gateway.conversation_id_from_payload(payload)
    with pytest.raises(ValueError, match="conversation_id must be a nonempty string"):
        dev_gateway.voice_conversation_id_from_payload(payload, "exact-call-id")


@pytest.mark.parametrize("value", [None, "", 7, [], {}])
def test_wrong_type_or_empty_conversation_id_is_rejected(value) -> None:
    with pytest.raises(
        ValueError,
        match="conversation_id must be a nonempty string",
    ):
        dev_gateway.conversation_id_from_payload(
            {"conversation_id": value, "session_id": "must-not-be-used"}
        )


def test_voice_conversation_id_is_not_derived_from_call_id() -> None:
    with pytest.raises(
        ValueError,
        match="conversation_id must be a nonempty string",
    ):
        dev_gateway.voice_conversation_id_from_payload({}, "exact-call-id")


@pytest.mark.asyncio
async def test_build_chat_response_dry_run_returns_fab_compatible_shape(tmp_path: Path) -> None:
    response = await dev_gateway.build_chat_response(
        {"conversation_id": "c1", "message": "Здравей"},
        settings(tmp_path),
    )

    assert response["status"] == "ok"
    assert response["version"] == dev_gateway.VERSION
    assert response["behavior_version"] == dev_gateway.SKYAI_BEHAVIOR_VERSION
    assert response["conversation_id"] == "c1"
    assert response["cards"] == []
    assert response["trace"]["runtime"] == "hermes_agent"
    assert response["trace"]["behavior_version"] == dev_gateway.SKYAI_BEHAVIOR_VERSION
    assert response["trace"]["toolset"] == "skyai_customer"
    assert response["trace"]["live_model"] is False
    assert "dry-run" in response["reply"]


@pytest.mark.asyncio
async def test_build_chat_response_requires_exact_external_conversation_id(
    tmp_path: Path,
) -> None:
    response = await dev_gateway.build_chat_response(
        {"message": "Здравей"},
        settings(tmp_path),
    )

    assert response["status"] == "error"
    assert response["error"] == "invalid_request"
    assert response["reason"] == "conversation_id must be a nonempty string"
    assert "conversation_id" not in response


@pytest.mark.asyncio
async def test_build_chat_response_allows_injected_runner(tmp_path: Path) -> None:
    seen = {}
    external_id = " \tThread/ß/А\n"
    message = " \tПокажи ми подарък\n"
    history = [{"role": "user", "content": "\nЗдравей \t"}]

    async def fake_runner(message, history, conversation_id, canary_settings):
        seen.update(
            {
                "message": message,
                "history": history,
                "conversation_id": conversation_id,
                "profile_home": canary_settings.profile_home,
            }
        )
        return {"final_response": "Отговор от тестов runner"}

    response = await dev_gateway.build_chat_response(
        {
            "conversation_id": external_id,
            "message": message,
            "history": history,
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["reply"] == "Отговор от тестов runner"
    assert response["conversation_id"] == external_id
    assert seen["message"] == message
    assert seen["history"] == history
    assert seen["conversation_id"] == hashlib.sha256(
        external_id.encode("utf-8")
    ).hexdigest()
    assert seen["profile_home"] == tmp_path / "profiles" / "skyai-v2-dev"


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["reply", "content", "message"])
async def test_runner_result_rejects_noncanonical_reply_aliases(
    tmp_path: Path,
    alias: str,
) -> None:
    async def fake_runner(*_args, **_kwargs):
        return {alias: "must not be repaired"}

    with pytest.raises(
        ValueError,
        match="runner result must contain final_response",
    ):
        await dev_gateway.build_chat_response(
            {"conversation_id": "c1", "message": "Здравей"},
            settings(tmp_path, live_model=True),
            agent_runner=fake_runner,
        )


@pytest.mark.asyncio
async def test_runner_result_preserves_final_response_exactly(tmp_path: Path) -> None:
    exact_reply = " \tОтговор от Hermes.\n"

    async def fake_runner(*_args, **_kwargs):
        return {"final_response": exact_reply}

    response = await dev_gateway.build_chat_response(
        {"conversation_id": "c1", "message": "Здравей"},
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["reply"] == exact_reply


@pytest.mark.asyncio
async def test_runner_error_prose_does_not_override_structured_completion_state(
    tmp_path: Path,
) -> None:
    exact_reply = " \tModel-authored response remains authoritative.\n"

    async def fake_runner(*_args, **_kwargs):
        return {
            "final_response": exact_reply,
            "completed": True,
            "failed": False,
            "error": "free-form prose that must not decide failure",
        }

    response = await dev_gateway.build_chat_response(
        {"conversation_id": "c1", "message": "Здравей"},
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["reply"] == exact_reply
    assert "error" not in response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner_result", "message"),
    [
        (
            {"final_response": "ok", "trace": {"provider": 7}},
            "runner trace provider must be a string",
        ),
        (
            {"final_response": "ok", "trace": {"fallback": "false"}},
            "runner trace fallback must be an exact boolean",
        ),
        (
            {"final_response": "ok", "failure_reason": ["rate_limit"]},
            "runner failure_reason must be a string",
        ),
        (
            {"final_response": "ok", "failed": "false"},
            "runner failed must be an exact boolean",
        ),
        (
            {"final_response": "ok", "completed": "true"},
            "runner completed must be an exact boolean",
        ),
        (
            {"final_response": "ok", "error": {"message": "alias"}},
            "runner error must be a string",
        ),
    ],
)
async def test_runner_metadata_rejects_wrong_typed_fields(
    tmp_path: Path,
    runner_result,
    message: str,
) -> None:
    async def fake_runner(*_args, **_kwargs):
        return runner_result

    with pytest.raises(ValueError, match=message):
        await dev_gateway.build_chat_response(
            {"conversation_id": "c1", "message": "Здравей"},
            settings(tmp_path, live_model=True),
            agent_runner=fake_runner,
        )


@pytest.mark.asyncio
async def test_build_chat_response_exposes_resolved_model_trace(tmp_path: Path) -> None:
    async def fake_runner(message, history, conversation_id, canary_settings):
        return {
            "final_response": "Отговор от Hermes.",
            "trace": {
                "model": "gpt-5.6-sol",
                "provider": "openai-codex",
                "api_mode": "codex_responses",
            },
        }

    response = await dev_gateway.build_chat_response(
        {"conversation_id": "c1", "message": "Здравей"},
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["trace"]["model"] == "gpt-5.6-sol"
    assert response["trace"]["provider"] == "openai-codex"
    assert response["trace"]["api_mode"] == "codex_responses"


@pytest.mark.asyncio
async def test_build_chat_response_fails_closed_on_provider_error(tmp_path: Path) -> None:
    async def fake_runner(message, history, conversation_id, canary_settings):
        return {
            "final_response": (
                "API call failed after 3 retries: HTTP 429: "
                "The usage limit has been reached"
            ),
            "completed": False,
            "failed": True,
            "error": "The usage limit has been reached",
            "failure_reason": "rate_limit",
            "trace": {
                "model": "gpt-5.6-sol",
                "provider": "openai-codex",
                "api_mode": "codex_responses",
                "credential_pool_size": 4,
                "credential_rotated": False,
            },
        }

    response = await dev_gateway.build_chat_response(
        {"conversation_id": "c1", "message": "Здравей"},
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "error"
    assert response["error"] == "provider_unavailable"
    assert response["reply"] == dev_gateway.SKYAI_MODEL_UNAVAILABLE_MESSAGE
    assert "429" not in response["reply"]
    assert "usage limit" not in response["reply"].lower()
    assert response["cards"] == []
    assert response["trace"]["failure_reason"] == "rate_limit"
    assert response["trace"]["credential_pool_size"] == 4
    assert response["trace"]["credential_rotated"] is False


@pytest.mark.asyncio
async def test_build_chat_response_passes_voice_system_prompt_to_hermes_runner(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings, system_prompt):
        seen.update(
            {
                "message": message,
                "conversation_id": conversation_id,
                "system_prompt": system_prompt,
            }
        )
        return {
            "final_response": "Говоря кратко, защото това е телефонен разговор."
        }

    response = await dev_gateway.build_chat_response(
        {
            "conversation_id": "voice-c1",
            "message": "Как мога да се свържа с екипа?",
            "metadata": {"surface": "pbx_voice", "source": "zycoo-coovox-u20"},
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
        surface="voice",
    )

    assert response["status"] == "ok"
    assert response["trace"]["surface"] == "voice"
    assert seen["message"] == "Как мога да се свържа с екипа?"
    assert seen["conversation_id"] == dev_gateway.runtime_conversation_id("voice-c1")
    assert "Voice режим" in seen["system_prompt"]
    assert "Клиентът вече се е свързал с официалната линия" in seen["system_prompt"]
    assert "не го връщай към 'официален канал'" in seen["system_prompt"]
    assert "не изброявай телефона, имейла или работното време" in seen["system_prompt"]
    assert "без markdown, сурови URL-и, дълги списъци" in seen["system_prompt"]
    assert "spoken_reply е авторитетният отговор" in seen["system_prompt"]
    assert "Скъсявай за телефон, но не променяй бизнес фактите" in seen["system_prompt"]
    assert "не казвай 'нека проверя'" in seen["system_prompt"]


@pytest.mark.asyncio
async def test_payload_fields_cannot_override_server_selected_chat_surface(
    tmp_path: Path,
) -> None:
    seen: dict[str, str] = {}

    async def fake_runner(message, history, conversation_id, canary_settings, system_prompt):
        seen["system_prompt"] = system_prompt
        return {"final_response": "Чат отговор."}

    response = await dev_gateway.build_chat_response(
        {
            "conversation_id": "chat-c1",
            "message": "Здравей",
            "surface": "pbx_voice",
            "call_id": "client-asserted-call",
            "metadata": {"surface": "pbx_voice"},
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["trace"]["surface"] == "chat"
    assert "Voice режим" not in seen["system_prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["Voice", "voice ", "pbx_voice", 1, None])
async def test_server_selected_surface_accepts_only_exact_enum(
    tmp_path: Path,
    surface,
) -> None:
    async def forbidden_runner(*_args, **_kwargs):
        raise AssertionError("invalid structural surface must not reach Hermes")

    with pytest.raises(ValueError, match="Unsupported SkyAI surface"):
        await dev_gateway.build_chat_response(
            {"conversation_id": "c1", "message": "Здравей"},
            settings(tmp_path, live_model=True),
            agent_runner=forbidden_runner,
            surface=surface,
        )


def test_create_app_registers_dev_routes(tmp_path: Path) -> None:
    app = dev_gateway.create_app(settings(tmp_path))
    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert ("GET", "/health") in routes
    assert ("GET", "/ready") in routes
    assert ("GET", "/version") in routes
    assert ("GET", "/widget/chatkit/") in routes
    assert ("POST", "/chatkit/dev-message") in routes
    assert ("POST", "/chatkit/message") in routes
    assert ("POST", "/qa/compare") in routes
    assert ("POST", "/voice/start") in routes
    assert ("POST", "/voice/turn") in routes
    assert ("POST", "/voice/event") in routes
    assert ("POST", "/voice/end") in routes


@pytest.mark.asyncio
async def test_health_exposes_configured_surface_mirror_marker(tmp_path: Path) -> None:
    app = dev_gateway.create_app(settings(tmp_path))
    health_route = next(
        route
        for route in app.router.routes()
        if route.method == "GET" and route.resource.canonical == "/health"
    )

    response = await health_route.handler(None)
    payload = json.loads(response.text)

    assert payload["implementation_markers"] == [
        dev_gateway.DISCORD_CONFIGURED_SURFACE_MIRROR_MARKER
    ]


@pytest.mark.asyncio
async def test_build_voice_start_response_returns_contract_shape(tmp_path: Path) -> None:
    response = await dev_gateway.build_voice_start_response(
        {
            "call_id": "call-1",
            "conversation_id": "voice-c1",
            "caller_id": "+35970020200",
            "pbx_extension": "399",
            "recording_notice_played": False,
        },
        settings(tmp_path),
    )

    assert response["status"] == "ok"
    assert response["contract_version"] == "skyai-voice-contract.v0.1"
    assert response["call_id"] == "call-1"
    assert response["conversation_id"] == "voice-c1"
    assert response["action"] == "speak"
    assert response["end_call"] is False
    assert response["transfer"] is None
    assert response["transfer_reason"] is None
    assert response["target"] is None
    assert response["notes"] == []
    assert response["unavailable"] is False
    assert response["trace"]["runtime"] == "skyai_voice_adapter"
    assert response["trace"]["raw_audio_stored"] is False
    assert response["trace"]["customer_mutations_allowed"] is False


@pytest.mark.asyncio
async def test_build_voice_turn_response_uses_v2_chat_adapter(tmp_path: Path) -> None:
    seen = {}
    exact_transcript = " \tТърся подарък за рожден ден.\n"

    async def fake_runner(message, history, conversation_id, canary_settings):
        seen.update(
            {
                "message": message,
                "history": history,
                "conversation_id": conversation_id,
                "profile_home": canary_settings.profile_home,
            }
        )
        return {
            "final_response": "Разбира се, ето идея за подарък.",
            "cards": [{"title": "Ваучер за подарък на стойност", "price_text": "стойност по избор"}],
        }

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-2",
            "conversation_id": "voice-c2",
            "turn_index": 1,
            "transcript": exact_transcript,
            "is_final": True,
            "stt_confidence": 0.91,
            "caller_id": "+35970020200",
            "did": "+35924259795",
            "pbx_extension": "399",
            "department": "sales",
            "language": "bg-BG",
            "source": "zycoo-coovox-u20",
            "history": [{"role": "assistant", "content": "Здравейте"}],
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert response["spoken_reply"] == "Разбира се, ето идея за подарък."
    assert not response["spoken_reply"].startswith("Извинете")
    assert response["display_reply"] == response["spoken_reply"]
    assert response["cards"] == [{"title": "Ваучер за подарък на стойност", "price_text": "стойност по избор"}]
    assert response["transfer"] is None
    assert response["transfer_reason"] is None
    assert response["target"] is None
    assert response["trace"]["backend_target"] == "skyai_v2_chatkit"
    assert response["trace"]["voice_backend_target"] == "skyai_v2_chatkit"
    assert response["trace"]["stt_confidence"] == 0.91
    assert seen["message"] == exact_transcript
    assert seen["history"] == [{"role": "assistant", "content": "Здравейте"}]
    assert seen["conversation_id"] == dev_gateway.runtime_conversation_id("voice-c2")


@pytest.mark.asyncio
async def test_voice_first_clear_turn_after_greeting_goes_to_hermes(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings, system_prompt):
        seen.update(
            {
                "message": message,
                "history": history,
                "system_prompt": system_prompt,
            }
        )
        return {
            "final_response": (
                "За спокоен рожден ден бих започнал с красив релакс подарък "
                "близо до Вас."
            )
        }

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-clear-first-turn",
            "conversation_id": "voice-clear-first-turn",
            "turn_index": 1,
            "transcript": "Търся подарък за рожден ден на приятелка, нещо спокойно.",
            "stt_confidence": 0.92,
            "history": [{"role": "assistant", "content": "Здравейте, свързахте се със SkyVision."}],
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert "Извинете, не Ви" not in response["spoken_reply"]
    assert "voice_reason" not in response["trace"]
    assert seen["message"] == "Търся подарък за рожден ден на приятелка, нещо спокойно."
    assert seen["history"] == [{"role": "assistant", "content": "Здравейте, свързахте се със SkyVision."}]
    assert "Voice режим" in seen["system_prompt"]


@pytest.mark.asyncio
async def test_voice_business_metadata_does_not_replace_semantic_reasoning(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings, system_prompt):
        seen.update({"message": message, "system_prompt": system_prompt})
        return {
            "final_response": (
                "Ще го подходим консултативно: повод, човек, локация и усещане."
            )
        }

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-domain-intent",
            "conversation_id": "voice-domain-intent",
            "transcript": "Моля за идея за подарък за рожден ден около Сливен.",
            "stt_confidence": 0.9,
            "metadata": {
                "surface": "pbx_voice",
                "domain_intent": "birthday_gift",
                "gift_intent": "true",
                "support_intent": "false",
            },
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert seen["message"] == "Моля за идея за подарък за рожден ден около Сливен."
    assert "domain_intent" not in seen["message"]
    assert "birthday_gift" not in seen["system_prompt"]
    assert "backend-ът няма phrase list" in seen["system_prompt"]


@pytest.mark.asyncio
async def test_voice_cheaper_than_voucher_value_keeps_residual_policy(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings, system_prompt):
        seen.update({"message": message, "system_prompt": system_prompt})
        return {
            "final_response": (
                "Ако избраното преживяване е по-евтино от стойността на ваучера, "
                "остатъкът остава като ваучерна стойност за следващо преживяване. "
                "Ако е по-скъпо, тогава се доплаща разликата."
            )
        }

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-residual-voucher",
            "conversation_id": "voice-residual-voucher",
            "transcript": "Какво става ако избраното преживяване е по-евтино от стойността на ваучера?",
            "stt_confidence": 0.93,
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert "остатъкът остава" in response["spoken_reply"]
    assert "ваучерна стойност" in response["spoken_reply"]
    assert seen["message"].startswith("Какво става ако избраното преживяване е по-евтино")
    assert "остатъкът остава като ваучерна стойност" in seen["system_prompt"]


@pytest.mark.asyncio
async def test_voice_follow_up_gets_history_instead_of_repeating_wrong_answer(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings, system_prompt):
        seen.update({"message": message, "history": history})
        return {
            "final_response": (
                "Точно така, при по-евтино преживяване остатъкът не се губи, "
                "а остава като ваучерна стойност."
            )
        }

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-follow-up",
            "conversation_id": "voice-follow-up",
            "transcript": "Не, имах предвид ако е по-евтино, не по-скъпо.",
            "stt_confidence": 0.96,
            "history": [
                {"role": "user", "content": "Какво ако е по-евтино?"},
                {"role": "assistant", "content": "Ще доплатите разликата."},
            ],
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert "остатъкът не се губи" in response["spoken_reply"]
    assert seen["message"] == "Не, имах предвид ако е по-евтино, не по-скъпо."
    assert seen["history"] == [
        {"role": "user", "content": "Какво ако е по-евтино?"},
        {"role": "assistant", "content": "Ще доплатите разликата."},
    ]


@pytest.mark.asyncio
async def test_voice_basic_policy_answer_does_not_force_checking_phrase(tmp_path: Path) -> None:
    async def fake_runner(message, history, conversation_id, canary_settings, system_prompt):
        return {
            "final_response": (
                "При BookNow, ако изпълнителят не може да проведе резервацията, "
                "парите ще бъдат възстановени."
            )
        }

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-basic-policy",
            "conversation_id": "voice-basic-policy",
            "transcript": "Какво става, ако времето е лошо и BookNow резервацията отпадне?",
            "stt_confidence": 0.94,
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert "парите ще бъдат възстановени" in response["spoken_reply"]
    assert "нека проверя" not in response["spoken_reply"].casefold()
    assert "проверявам" not in response["spoken_reply"].casefold()


@pytest.mark.asyncio
async def test_build_voice_turn_preserves_model_authored_reply_exactly(
    tmp_path: Path,
) -> None:
    exact_reply = (
        " \tЕто подробности: [Полет](https://skyvision.bg/example) е чудесен избор.\n"
        + ("Много красив подарък. " * 80)
    )

    async def fake_runner(*args, **kwargs):
        return {
            "final_response": exact_reply,
            "cards": [],
        }

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-tts",
            "conversation_id": "voice-tts",
            "transcript": "Разкажи ми повече за този подарък.",
            "stt_confidence": 0.95,
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert response["spoken_reply"] == exact_reply
    assert response["display_reply"] == exact_reply


@pytest.mark.asyncio
async def test_build_voice_turn_low_confidence_clarifies_without_model_call(tmp_path: Path) -> None:
    async def forbidden_runner(*args, **kwargs):
        raise AssertionError("low-confidence voice turns must not call Hermes")

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-3",
            "conversation_id": "voice-c3",
            "transcript": "шшш",
            "stt_confidence": 0.2,
        },
        settings(tmp_path, live_model=True),
        agent_runner=forbidden_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "clarify"
    assert "повторите" in response["spoken_reply"]
    assert response["trace"]["voice_reason"] == "low_stt_confidence"
    assert response["trace"]["raw_audio_stored"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload_update", "error"),
    [
        ({"stt_confidence": "0.2"}, "stt_confidence must be a number or null"),
        ({"silence_count": "2"}, "silence_count must be an integer or null"),
        ({"dtmf": 0}, "dtmf must be a string"),
        ({"transcript": ["spoken alias"]}, "transcript must be a string"),
    ],
)
async def test_voice_turn_rejects_protocol_coercion(
    tmp_path: Path,
    payload_update: dict,
    error: str,
) -> None:
    payload = {
        "call_id": "call-strict-protocol",
        "conversation_id": "voice-strict-protocol",
        "transcript": "",
    }
    payload.update(payload_update)

    with pytest.raises(ValueError, match=error):
        await dev_gateway.build_voice_turn_response(
            payload,
            settings(tmp_path, live_model=True),
        )


@pytest.mark.asyncio
async def test_build_voice_turn_dtmf_zero_transfers_without_model_call(tmp_path: Path) -> None:
    async def forbidden_runner(*args, **kwargs):
        raise AssertionError("DTMF 0 must transfer without calling Hermes")

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-dtmf",
            "conversation_id": "voice-dtmf",
            "dtmf": "0",
            "transcript": "",
            "stt_confidence": 0.99,
        },
        settings(tmp_path, live_model=True),
        agent_runner=forbidden_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "transfer_to_human"
    assert response["transfer"] == {"target": "operator_queue", "reason": "dtmf_0"}
    assert response["transfer_reason"] == "dtmf_0"
    assert response["target"] == "operator_queue"


@pytest.mark.asyncio
async def test_build_voice_turn_human_request_transfers_from_structured_hermes_action(tmp_path: Path) -> None:
    seen = {}
    exact_reason = " \thermes_requested_handoff\n"
    exact_spoken_reply = " \tРазбира се, ще Ви прехвърля към колега.\n"
    exact_display_reply = "\nHermes requested human handoff.\t"

    async def fake_runner(message, history, conversation_id, canary_settings):
        seen["message"] = message
        return {
            "final_response": exact_spoken_reply,
            "messages": [
                {
                    "role": "tool",
                    "content": json.dumps(
                        {
                            "status": "ok",
                            "voice_action": "transfer_to_human",
                            "transfer": {
                                "target": "operator_queue",
                                "reason": exact_reason,
                            },
                            "spoken_reply": exact_spoken_reply,
                            "display_reply": exact_display_reply,
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
        }

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-human",
            "conversation_id": "voice-human",
            "transcript": "Моля, свържете ме с човек от екипа.",
            "stt_confidence": 0.94,
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "transfer_to_human"
    assert response["spoken_reply"] == exact_spoken_reply
    assert response["display_reply"] == exact_display_reply
    assert response["transfer_reason"] == exact_reason
    assert response["target"] == "operator_queue"
    assert response["trace"]["voice_action_source"] == "hermes_tool"
    assert seen["message"] == "Моля, свържете ме с човек от екипа."


@pytest.mark.parametrize(
    "payload",
    [
        {
            "voice_action": "transfer_to_human",
            "reason": "top-level alias",
            "transfer": {"target": "operator_queue"},
            "spoken_reply": "Ще Ви прехвърля.",
            "display_reply": "Transfer.",
        },
        {
            "voice_action": "transfer_to_human",
            "transfer": {"target": "operator_queue", "reason": 7},
            "spoken_reply": "Ще Ви прехвърля.",
            "display_reply": "Transfer.",
        },
        {
            "voice_action": "transfer_to_human",
            "transfer": {
                "target": "operator_queue",
                "reason": "exact",
                "label": "unsupported",
            },
            "spoken_reply": "Ще Ви прехвърля.",
            "display_reply": "Transfer.",
        },
    ],
)
def test_structured_voice_action_rejects_aliases_and_wrong_types(payload) -> None:
    with pytest.raises(ValueError):
        dev_gateway._coerce_voice_action_payload(payload)


@pytest.mark.asyncio
async def test_build_voice_turn_does_not_transfer_for_ordinary_person_words(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings):
        seen["message"] = message
        return {
            "final_response": (
                "Подходящ подарък за този човек може да е ваучер на стойност."
            )
        }

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-person",
            "conversation_id": "voice-person",
            "transcript": "Търся подарък за спокоен човек.",
            "stt_confidence": 0.94,
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert response["transfer"] is None
    assert seen["message"] == "Търся подарък за спокоен човек."


@pytest.mark.asyncio
async def test_build_voice_turn_repeated_silence_clarifies_without_model_call(tmp_path: Path) -> None:
    async def forbidden_runner(*args, **kwargs):
        raise AssertionError("silence turns must not call Hermes")

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-silence",
            "conversation_id": "voice-silence",
            "transcript": "",
            "silence_count": 2,
        },
        settings(tmp_path, live_model=True),
        agent_runner=forbidden_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "clarify"
    assert response["trace"]["voice_reason"] == "silence_timeout"
    assert response["trace"]["silence_count"] == 2


@pytest.mark.asyncio
async def test_build_voice_event_dtmf_zero_transfers_to_human(tmp_path: Path) -> None:
    response = await dev_gateway.build_voice_event_response(
        {
            "call_id": "call-4",
            "conversation_id": "voice-c4",
            "event_type": "dtmf",
            "dtmf": "0",
        },
        settings(tmp_path),
    )

    assert response["status"] == "ok"
    assert response["action"] == "transfer_to_human"
    assert response["transfer"] == {"target": "operator_queue", "reason": "dtmf_0"}
    assert response["target"] == "operator_queue"
    assert response["transfer_reason"] == "dtmf_0"
    assert "човек" in response["spoken_reply"]


@pytest.mark.asyncio
async def test_voice_event_identifier_is_not_trimmed_casefolded_or_aliased(
    tmp_path: Path,
) -> None:
    exact_event_type = " Caller_Requested_Human "
    response = await dev_gateway.build_voice_event_response(
        {
            "call_id": "call-exact-event",
            "conversation_id": "voice-exact-event",
            "event_type": exact_event_type,
            "event": "caller_requested_human",
            "metadata": {"dtmf": "0"},
        },
        settings(tmp_path),
    )

    assert response["action"] == "clarify"
    assert response["transfer"] is None
    assert response["trace"]["voice_event"] == exact_event_type


@pytest.mark.asyncio
async def test_build_voice_turn_v1_target_requires_configured_backend(tmp_path: Path) -> None:
    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-5",
            "conversation_id": "voice-c5",
            "backend_target": "skyai_v1_chatkit",
            "transcript": "Искам ваучер.",
            "stt_confidence": 0.9,
        },
        settings(tmp_path),
    )

    assert response["status"] == "ok"
    assert response["action"] == "transfer_to_human"
    assert response["transfer"] == {
        "target": "operator_queue",
        "reason": "voice_v1_backend_not_configured",
    }
    assert response["target"] == "operator_queue"
    assert response["transfer_reason"] == "voice_v1_backend_not_configured"
    assert response["trace"]["backend_target"] == "skyai_v1_chatkit"


@pytest.mark.asyncio
async def test_build_voice_turn_invalid_backend_target_transfers_to_human(tmp_path: Path) -> None:
    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-invalid",
            "conversation_id": "voice-invalid",
            "backend_target": "unknown",
            "transcript": "Здравейте",
            "stt_confidence": 0.9,
        },
        settings(tmp_path),
    )

    assert response["status"] == "error"
    assert response["action"] == "transfer_to_human"
    assert response["transfer"] == {"target": "operator_queue", "reason": "invalid_voice_backend_target"}
    assert response["transfer_reason"] == "invalid_voice_backend_target"
    assert response["target"] == "operator_queue"
    assert response["unavailable"] is True


@pytest.mark.asyncio
async def test_build_voice_end_response_ends_call_without_mutations(tmp_path: Path) -> None:
    response = await dev_gateway.build_voice_end_response(
        {
            "call_id": "call-6",
            "conversation_id": "voice-c6",
            "ended_by": "caller",
            "duration_seconds": 42,
            "recording_stored": False,
            "transcript_stored": True,
        },
        settings(tmp_path),
    )

    assert response["status"] == "ok"
    assert response["action"] == "end_call"
    assert response["end_call"] is True
    assert response["trace"]["ended_by"] == "caller"
    assert response["trace"]["duration_seconds"] == 42
    assert response["trace"]["recording_stored"] is False
    assert response["trace"]["transcript_stored"] is True
    assert response["trace"]["customer_mutations_allowed"] is False


@pytest.mark.asyncio
async def test_voice_end_preserves_exact_state_identifier_and_rejects_bool_lookalikes(
    tmp_path: Path,
) -> None:
    response = await dev_gateway.build_voice_end_response(
        {
            "call_id": "call-exact-end",
            "conversation_id": "voice-exact-end",
            "ended_by": " caller ",
        },
        settings(tmp_path),
    )
    assert response["trace"]["ended_by"] == " caller "

    with pytest.raises(ValueError, match="recording_stored must be an exact boolean"):
        await dev_gateway.build_voice_end_response(
            {
                "call_id": "call-invalid-end",
                "conversation_id": "voice-invalid-end",
                "recording_stored": "false",
            },
            settings(tmp_path),
        )


def test_render_widget_html_contains_fab_compatible_chat_endpoint(tmp_path: Path) -> None:
    html = dev_gateway.render_widget_html(settings(tmp_path, version="test-version"))

    assert "<title>SkyAI асистент | SkyVision</title>" in html
    assert 'meta name="skyvision-clean-dev-version" content="test-version"' in html
    assert "<h1>SkyAI асистент</h1>" in html
    assert "#32BCAD" in html
    assert "#275E7C" in html
    assert "test-version" in html
    assert "fetch('/chatkit/message'" in html
    assert "message--typing" in html
    assert "card__image" in html
    assert "appendCards(payload.cards)" in html
    assert "skyai-widget-transcript:" in html
    assert "function persistTranscript" in html
    assert "function restoreTranscript" in html
    assert "const history = state.turns.slice(-maxHistoryTurns);" in html
    assert "async function sendMessage(message, history, deliveryId)" in html
    assert "window.crypto.randomUUID()" in html
    assert "delivery_id: deliveryId" in html
    assert "void sendMessage(message, history, deliveryId);" in html
    assert "messages: state.turns" not in html
    assert "const message = elements.input.value;" in html
    assert "const message = elements.input.value.trim();" not in html
    assert "appendMessage(item.role, item.text, { persist: false })" in html
    assert "appendCards(item.cards, { persist: false })" in html
    assert "renderAssistantMarkdown" in html
    assert "message--rich" in html
    assert "node.innerHTML = renderAssistantMarkdown(text)" in html
    assert "escapeHtml" in html
    assert "message__heading" in html
    assert "line.match(/^#{1,4}" in html
    assert "document.addEventListener('click'" in html
    assert 'target="_top"' in html
    assert "anchor.target = '_top'" in html
    assert "window.location.href = href" not in html
    assert "function isTestSession" not in html
    assert "X-SkyAI-Test-Signal" not in html


def test_gateway_transport_reconciles_message_by_exact_nonce_history(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_array_request(method: str, path: str, token: str):
        calls.append((method, path, token))
        return [
            {"id": "message-other", "nonce": "other"},
            {"id": "message-exact", "nonce": "ExactNonce"},
        ]

    monkeypatch.setattr(
        dev_gateway,
        "_discord_json_array_request",
        fake_array_request,
    )
    transport = dev_gateway.GatewayDiscordTransport("bot-token")

    assert transport.find_message_ids_by_exact_nonce(
        "thread-1",
        "ExactNonce",
    ) == ["message-exact"]
    assert calls == [
        (
            "GET",
            "/channels/thread-1/messages?limit=100",
            "bot-token",
        )
    ]


def test_system_prompt_links_campaign_bonus_id_to_slots_tool() -> None:
    prompt = dev_gateway.build_skyai_system_prompt()

    assert "Hermes мисли" in prompt
    assert "не вземат customer-visible семантични решения" in prompt
    assert "никакъв tool output като готова реплика" in prompt
    assert "скрита класификация" in prompt
    assert "skyai_product_slots" in prompt
    assert "skyai_support_knowledge" in prompt
    assert "клиентския панел „Ваучери“" in prompt
    assert "добавяне/управление на ваучери" in prompt
    assert "EUR е основната цена" in prompt
    assert "Catalog tool-ът изпраща твоята заявка" in prompt
    assert "публичния API" in prompt
    assert "backend ред" in prompt
    assert "Сам интерпретирай заявката и резултатите" in prompt
    assert "Hermes сам носи отговорност" in prompt
    assert "няма display-level card adapter" in prompt
    assert "selection_context/category_mix" not in prompt
    assert "не пълни" in prompt
    assert "не приемай автоматично" in prompt
    assert "първо мисли близко" in prompt
    assert "nearest_returned_items" not in prompt
    assert "ти решаваш дали да уточниш или да разшириш" in prompt
    assert "започни направо с желаната посока" in prompt
    assert "positive-only" in prompt
    assert "не използвай конструкции от типа 'без X/Y'" in prompt
    assert "бонусът е благодарност към купувача/резервиращия" in prompt
    assert "бонусният полет се изпълнява единствено от летище Приморско" in prompt
    assert "независимо къде е основната закупена услуга" in prompt
    assert "по правило бонусът е за купувача/резервиращия" in prompt
    assert "акаунта/данните му" in prompt
    assert "не се прехвърля автоматично" in prompt
    assert "Емил Ломлиев" in prompt
    assert "съосновател с Малина през 2007" in prompt
    assert "пилот-инструктор" in prompt
    assert "+359 886 417 142" in prompt
    assert "Подаръчните бонуси нямат ваучерен/сериен номер" in prompt
    assert "не се добавят ръчно" in prompt
    assert "ако купувачът е логнат" in prompt
    assert "автоматично в профила" in prompt
    assert "се обвързва с имейла от поръчката" in prompt
    assert "профил със същия имейл" in prompt
    assert "не започвай с директно 'да'" in prompt
    assert "не представяй бонуса като подарък за получателя" in prompt
    assert "BookNow е директна резервация" in prompt
    assert "парите ще бъдат възстановени" in prompt
    assert "не като несигурна възможност" in prompt
    assert "не загатвай, че можеш да завършиш заявка" in prompt
    assert "Клиентът трябва сам да отвори" in prompt
    assert "продуктовия public_url" in prompt
    assert "Историята е общ контекст" in prompt
    assert "Отговаряй само с новото" in prompt
    assert "сравни всяко твърдение и стъпка" in prompt
    assert "ако смисълът вече е даден, изтрий го" in prompt
    assert "Полезността или свързаността не оправдава повторение" in prompt
    assert "поправка/недоволство" in prompt
    assert "поправи само новото" in prompt
    assert "изрично искане или корекция" in prompt
    assert "само нужната част" in prompt
    assert "Два ваучера не се обединяват автоматично" in prompt
    assert "остатъкът остава като ваучерна стойност" in prompt
    assert "Опцията за удължаване е налична" in prompt
    assert "customer-safe обучение от реални email/support казуси" in prompt
    assert "intent/state reasoning, а не като шаблон" in prompt
    assert "приемай неуточнения ваучер за ваучер на SkyVision" in prompt
    assert "не питай рутинно за издателя" in prompt
    assert "конкретна причина да се съмняваш в съвместимостта" in prompt
    assert "Само ваучерите на SkyVision важат в SkyVision профила" in prompt
    assert "Ако клиентът посочи друг издател" in prompt
    assert "ваучерът не може да се добави тук" in prompt
    assert "се обслужва от издателя си" in prompt
    assert "при неясен произход първо го уточни" not in prompt
    assert "Писмен контакт с екипа: info@skyvision.bg" in prompt
    assert "reservations@skyvision.bg е автоматичен адрес" in prompt
    assert "не канал за клиентски отговори" in prompt
    assert "не коментирай самото ограничение" in prompt
    assert "представи се кратко като SkyAI" in prompt
    assert "не решавай учебни задачи" in prompt
    assert len(prompt) < 6300
    assert "силно попадение" not in prompt
    assert "ще й легне" not in prompt
    assert "ако клиентът уточни нещо" not in prompt


@pytest.mark.asyncio
async def test_reply_prose_never_generates_product_cards(tmp_path: Path) -> None:
    exact_reply = (
        "Виж https://skyvision.bg/подарък/полет-с-жирокоптер/"
        "полет-с-жирокоптер-mto-sport/"
    )

    async def fake_runner(*_args, **_kwargs):
        return {"final_response": exact_reply}

    response = await dev_gateway.build_chat_response(
        {"conversation_id": "exact-cardless", "message": "Покажи ми полет."},
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["reply"] == exact_reply
    assert response["cards"] == []


def test_canonical_cards_preserve_exact_typed_values() -> None:
    card = {
        "title": " \tПолет 🧪\n",
        "url": " https://skyvision.bg/exact ",
        "price_eur": " 101.75 ",
        "location": "\nПриморско\t",
        "image": " https://cdn.example/gyro.jpg ",
    }

    assert dev_gateway._validate_cards([card]) == [card]


@pytest.mark.parametrize(
    "card",
    [
        {"name": "alias"},
        {"title": "exact", "public_url": "https://skyvision.bg/alias"},
        {"title": "exact", "image_url": "https://cdn.example/alias.jpg"},
        {"title": 7},
        {"title": ""},
    ],
)
def test_cards_reject_aliases_wrong_types_and_empty_title(card) -> None:
    with pytest.raises(ValueError):
        dev_gateway._validate_cards([card])


def test_dev_gateway_has_no_display_level_card_adapter() -> None:
    source = Path(dev_gateway.__file__).read_text(encoding="utf-8")

    assert "_select_visible_cards" not in source
    assert "_card_similarity_score" not in source
    assert "MAX_CARD_CANDIDATE_LINKS" not in source
    assert "trace.customer_model" not in source
    assert "trace.model_lane" not in source
    assert "trace.auth_route" not in source
    assert "card.image_url || card.image" not in source
    assert ".trim()" not in source
    assert "String(error.message)" not in source
    assert "re.sub(" not in source


def test_resolve_profile_runtime_reads_model_dict() -> None:
    runtime = dev_gateway._resolve_profile_runtime(
        {
            "model": {
                "default": "gpt-5.6-sol",
                "provider": "openai-codex",
                "base_url": "https://chatgpt.com/backend-api/codex",
                "api_mode": "codex_responses",
            }
        }
    )

    assert runtime == {
        "model": "gpt-5.6-sol",
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
        "api_key": "",
    }


def test_resolve_profile_runtime_preserves_exact_identifier_bytes() -> None:
    runtime = dev_gateway._resolve_profile_runtime(
        {
            "model": {
                "default": " gpt-5.6-sol ",
                "provider": " OpenAI-Codex ",
                "base_url": " https://example.invalid/exact ",
                "api_mode": " codex_responses ",
            }
        }
    )

    assert runtime == {
        "model": " gpt-5.6-sol ",
        "provider": " OpenAI-Codex ",
        "base_url": " https://example.invalid/exact ",
        "api_mode": " codex_responses ",
        "api_key": "",
    }


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"model": "gpt-5.6-sol"}, "model config must be an object"),
        ({"model": {"default": 7}}, "model.default must be a string"),
        ({"model": {"provider": ["openai-codex"]}}, "model.provider must be a string"),
    ],
)
def test_resolve_profile_runtime_rejects_legacy_and_wrong_typed_fields(
    config,
    message,
) -> None:
    with pytest.raises(ValueError, match=message):
        dev_gateway._resolve_profile_runtime(config)


def test_resolve_agent_runtime_refreshes_codex_credentials() -> None:
    seen = {}

    def fake_codex_resolver(**kwargs):
        seen.update(kwargs)
        return {
            "api_key": "fresh-oauth-token",
            "base_url": "https://chatgpt.com/backend-api/codex",
        }

    runtime = dev_gateway._resolve_agent_runtime(
        {
            "model": {
                "default": "gpt-5.6-sol",
                "provider": "openai-codex",
                "api_mode": "codex_responses",
            }
        },
        codex_credential_resolver=fake_codex_resolver,
    )

    assert seen == {"refresh_if_expiring": True}
    assert runtime == {
        "model": "gpt-5.6-sol",
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
        "api_key": "fresh-oauth-token",
    }


def test_resolve_agent_runtime_attaches_profile_credential_pool() -> None:
    pool = object()
    seen = {}

    def fake_runtime_resolver(**kwargs):
        seen.update(kwargs)
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "selected-pool-token",
            "credential_pool": pool,
        }

    runtime = dev_gateway._resolve_agent_runtime(
        {
            "model": {
                "default": "gpt-5.6-sol",
                "provider": "openai-codex",
                "api_mode": "codex_responses",
            }
        },
        runtime_provider_resolver=fake_runtime_resolver,
    )

    assert seen == {
        "requested": "openai-codex",
        "target_model": "gpt-5.6-sol",
    }
    assert runtime["api_key"] == "selected-pool-token"
    assert runtime["credential_pool"] is pool


def test_resolve_agent_runtime_rejects_provider_id_rewrite() -> None:
    def mismatched_runtime_resolver(**_kwargs):
        return {
            "provider": "OpenAI-Codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "token",
        }

    with pytest.raises(
        ValueError,
        match="changed the exact configured provider id",
    ):
        dev_gateway._resolve_agent_runtime(
            {
                "model": {
                    "default": "gpt-5.6-sol",
                    "provider": "openai-codex",
                    "api_mode": "codex_responses",
                }
            },
            runtime_provider_resolver=mismatched_runtime_resolver,
        )


def test_sanitize_runtime_error_preserves_lookalikes_and_redacts_registered_values(
    monkeypatch,
) -> None:
    registered_secret = "registered-secret-value-123456"
    exact_text = (
        " Bearer abc123\n"
        "access_token=lookalike refresh_token:lookalike2 api_key=lookalike3 "
        f"before:{registered_secret}:after"
    )
    monkeypatch.setenv("SKYAI_V2_CANARY_TOKEN", registered_secret)

    assert dev_gateway.sanitize_runtime_error(RuntimeError(exact_text)) == (
        " Bearer abc123\n"
        "access_token=lookalike refresh_token:lookalike2 api_key=lookalike3 "
        "before:[redacted-secret]:after"
    )


def test_sanitize_runtime_error_redacts_exact_settings_secret_without_key_scanning(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SKYAI_V2_CANARY_TOKEN", raising=False)
    settings_secret = "exact-settings-secret"

    assert dev_gateway.sanitize_runtime_error(
        RuntimeError(f"opaque={settings_secret} api_key=unregistered-lookalike"),
        registered_secrets=(settings_secret,),
    ) == "opaque=[redacted-secret] api_key=unregistered-lookalike"


def test_format_discord_mirror_message_uses_customer_visible_shape() -> None:
    message = dev_gateway.format_discord_mirror_message(
        {"conversation_id": "c1", "message": "Търся подарък"},
        {
            "status": "ok",
            "version": "v-test",
            "behavior_version": "v2.1",
            "conversation_id": "c1",
            "reply": "Имаме чудесни идеи.",
            "trace": {
                "runtime": "hermes_agent",
                "toolset": "skyai_customer",
                "live_model": True,
                "fallback": False,
                "latency_ms": 12,
            },
        },
    )

    assert "**Клиент**" in message
    assert "**SkyAI**" in message
    assert "Търся подарък" in message
    assert "Имаме чудесни идеи." in message
    assert "version=v2.1" in message
    assert "runtime_version=v-test" in message
    assert "toolset=skyai_customer" in message


def test_format_discord_mirror_message_does_not_classify_request_content() -> None:
    message = dev_gateway.format_discord_mirror_message(
        {
            "conversation_id": "test-qa-smoke-preview-dev",
            "message": "QA тест",
            "origin_class": "test",
            "is_test": True,
            "metadata": {
                "page_referrer": "https://skyvision.bg/?skyai_test=1",
                "client_ip": "127.0.0.1",
            },
        },
        {
            "status": "ok",
            "version": "v-test",
            "conversation_id": "test-qa-smoke-preview-dev",
            "reply": "Тестов отговор.",
            "trace": {
                "runtime": "hermes_agent",
                "toolset": "skyai_customer",
                "live_model": True,
                "fallback": False,
                "latency_ms": 12,
            },
        },
    )

    assert message.startswith("**SkyAI v2 canary · test-qa-smoke-preview-dev**")
    assert "origin_class" not in message
    assert "origin_reason" not in message
    assert "🧪" not in message


@pytest.mark.parametrize(
    "response",
    [
        {"status": "error", "reason": "must-not-alias", "trace": {}},
        {"status": "error", "error": "must-not-alias", "trace": {}},
        {"status": "ok", "reply": 7, "trace": {}},
    ],
)
def test_chat_mirror_requires_exact_canonical_reply(response: dict) -> None:
    with pytest.raises(ValueError, match="reply"):
        dev_gateway.format_discord_mirror_message(
            {"conversation_id": "c1", "message": "exact"},
            response,
        )


def test_discord_version_does_not_alias_behavior_version_from_trace() -> None:
    message = dev_gateway.format_discord_mirror_message(
        {"conversation_id": "c1", "message": "exact"},
        {
            "status": "ok",
            "version": "runtime-v",
            "reply": "exact",
            "trace": {"behavior_version": "must-not-alias"},
        },
    )

    assert "must-not-alias" not in message
    assert "runtime_version=runtime-v" in message


def test_discord_version_rejects_wrong_typed_canonical_field() -> None:
    with pytest.raises(ValueError, match="behavior_version must be a string"):
        dev_gateway.format_discord_mirror_message(
            {"conversation_id": "c1", "message": "exact"},
            {
                "status": "ok",
                "behavior_version": ["v2.7"],
                "reply": "exact",
                "trace": {},
            },
        )


@pytest.mark.asyncio
async def test_chat_mirror_splits_losslessly_without_truncating_authored_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    exact_customer_text = " \tКлиент\n" + ("абв🙂\n" * 900) + "  "
    exact_reply = "\n\tSkyAI\n" + ("отговор🧪\t" * 650) + "\n "
    payload = {
        "conversation_id": "lossless-chat",
        "message": exact_customer_text,
    }
    response = {
        "status": "ok",
        "version": "v-test",
        "conversation_id": "lossless-chat",
        "reply": exact_reply,
        "trace": {},
    }
    posted_chunks: list[str] = []

    async def fake_target_channel_id(**_kwargs) -> str:
        return "thread-lossless"

    def fake_post_message(
        channel_id: str,
        token: str,
        content: str,
    ) -> dict[str, str]:
        assert channel_id == "thread-lossless"
        posted_chunks.append(content)
        return {"id": f"message-{len(posted_chunks)}"}

    monkeypatch.setattr(
        dev_gateway,
        "_discord_target_channel_id",
        fake_target_channel_id,
    )
    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)

    expected = dev_gateway.format_discord_mirror_message(payload, response)
    result = await dev_gateway.mirror_to_discord(
        payload,
        response,
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id=dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
            discord_mirror_create_threads=True,
        ),
    )

    assert exact_customer_text in expected
    assert exact_reply in expected
    assert len(posted_chunks) > 1
    assert all(
        0
        < len(chunk.encode("utf-16-le", errors="surrogatepass")) // 2
        <= 2000
        for chunk in posted_chunks
    )
    assert "".join(posted_chunks) == expected
    assert result["message_ids"] == [
        f"message-{index}" for index in range(1, len(posted_chunks) + 1)
    ]
    assert result["message_count"] == len(posted_chunks)


@pytest.mark.asyncio
async def test_discord_thread_name_is_structural_not_inferred_from_conversation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    starter_messages: list[str] = []
    created_names: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        starter_messages.append(content)
        return {"id": "starter-message"}

    def fake_start_thread(channel_id: str, message_id: str, token: str, name: str) -> dict[str, str]:
        created_names.append(name)
        return {"id": "thread-1"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(dev_gateway, "_discord_start_thread_from_message", fake_start_thread)

    thread_id = await dev_gateway._discord_target_channel_id(
        settings=settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id=dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "threads.json",
        ),
        conversation_id="test-qa-smoke-preview-dev",
    )

    assert thread_id == "thread-1"
    assert starter_messages == ["SkyAI v2 разговор `test-qa-smoke-preview-dev`"]
    assert created_names == ["SkyAI v2 · test-qa-smoke-preview-dev"]


@pytest.mark.asyncio
async def test_mirror_to_discord_skips_when_disabled(tmp_path: Path) -> None:
    result = await dev_gateway.mirror_to_discord(
        {"message": "Здравей"},
        {"status": "ok", "reply": "Здравей", "trace": {}},
        settings(tmp_path),
    )

    assert result == {"status": "skipped", "reason": "disabled"}


def test_format_voice_discord_mirror_message_uses_structural_voice_surface() -> None:
    message = dev_gateway.format_voice_discord_mirror_message(
        {
            "call_id": "call-voice-1",
            "conversation_id": "voice-c1",
            "transcript": "Искам оператор.",
            "caller_id": "+35970020200",
            "did": "+35924259795",
            "pbx_extension": "399",
            "language": "bg-BG",
            "source": "zycoo-coovox-u20",
            "stt_confidence": 0.96,
        },
        {
            "status": "ok",
            "version": "v-test",
            "contract_version": "skyai-voice-contract.v0.1",
            "call_id": "call-voice-1",
            "conversation_id": "voice-c1",
            "action": "transfer_to_human",
            "spoken_reply": "Свързвам Ви с оператор.",
            "display_reply": "Caller requested handoff.",
            "transfer": {"target": "operator_queue", "reason": "caller_requested_human"},
            "end_call": False,
            "trace": {
                "runtime": "skyai_voice_adapter",
                "backend_target": "skyai_v2_chatkit",
                "raw_audio_stored": False,
            },
        },
        stage="turn",
    )

    assert message.startswith("**🎙️ Voice SkyAI разговор**")
    assert "**Клиент / STT**" in message
    assert "Искам оператор." in message
    assert "**SkyAI / spoken**" in message
    assert "Свързвам Ви с оператор." in message
    assert "pbx_extension=399" in message
    assert "stage=turn" in message
    assert "action=transfer_to_human" in message
    assert "origin_class" not in message


def test_voice_discord_format_preserves_authored_fields_and_splits_losslessly() -> None:
    transcript = " \tИскам оператор.\n"
    spoken_reply = "\n Свързвам Ви с оператор.\t"
    display_reply = " \tCaller requested handoff.\n"
    message = dev_gateway.format_voice_discord_mirror_message(
        {
            "call_id": "call-voice-exact",
            "conversation_id": "voice-exact",
            "transcript": transcript,
        },
        {
            "status": "ok",
            "call_id": "call-voice-exact",
            "conversation_id": "voice-exact",
            "spoken_reply": spoken_reply,
            "display_reply": display_reply,
            "trace": {},
        },
    )
    chunks = dev_gateway._split_discord_message(message, limit=37)

    assert transcript in message
    assert spoken_reply in message
    assert display_reply in message
    assert all(
        0 < len(chunk.encode("utf-16-le", errors="surrogatepass")) // 2 <= 37
        for chunk in chunks
    )
    assert "".join(chunks) == message


def test_voice_discord_metadata_rejects_stringified_lookalikes() -> None:
    with pytest.raises(ValueError, match="turn_index must be a nonnegative integer"):
        dev_gateway.format_voice_discord_mirror_message(
            {
                "call_id": "call-exact",
                "conversation_id": "voice-exact",
                "transcript": "exact",
                "turn_index": "1",
            },
            {
                "status": "ok",
                "call_id": "call-exact",
                "conversation_id": "voice-exact",
                "spoken_reply": "exact",
                "display_reply": "exact",
                "trace": {},
            },
        )

    with pytest.raises(ValueError, match="end_call must be an exact boolean"):
        dev_gateway.format_voice_discord_mirror_message(
            {
                "call_id": "call-exact",
                "conversation_id": "voice-exact",
                "transcript": "exact",
            },
            {
                "status": "ok",
                "call_id": "call-exact",
                "conversation_id": "voice-exact",
                "spoken_reply": "exact",
                "display_reply": "exact",
                "end_call": "false",
                "trace": {},
            },
        )


def test_voice_discord_service_fields_do_not_use_aliases() -> None:
    message = dev_gateway.format_voice_discord_mirror_message(
        {
            "call_id": "call-exact",
            "conversation_id": "voice-exact",
            "transcript": "exact",
            "stt_confidence": 0.99,
        },
        {
            "status": "ok",
            "call_id": "call-exact",
            "conversation_id": "voice-exact",
            "spoken_reply": "exact",
            "display_reply": "exact",
            "target": "must-not-alias",
            "transfer_reason": "must-not-alias",
            "trace": {
                "voice_backend_target": "must-not-alias",
                "contract_version": "must-not-alias",
            },
        },
    )

    assert "must-not-alias" not in message
    assert "stt_confidence=0.99" not in message


def test_voice_backend_target_ignores_metadata_alias_and_preserves_exact_id(
    tmp_path: Path,
) -> None:
    canary_settings = settings(
        tmp_path,
        voice_backend_target="skyai_v2_chatkit",
    )

    assert dev_gateway._voice_backend_target(
        {"metadata": {"backend_target": "skyai_v1_chatkit"}},
        canary_settings,
    ) == "skyai_v2_chatkit"
    assert dev_gateway._voice_backend_target(
        {"backend_target": " skyai_v1_chatkit "},
        canary_settings,
    ) == " skyai_v1_chatkit "


@pytest.mark.asyncio
async def test_discord_thread_name_marks_voice_threads(tmp_path: Path, monkeypatch) -> None:
    starter_messages: list[str] = []
    created_names: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        starter_messages.append(content)
        return {"id": "starter-message"}

    def fake_start_thread(channel_id: str, message_id: str, token: str, name: str) -> dict[str, str]:
        created_names.append(name)
        return {"id": "voice-thread-1"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(dev_gateway, "_discord_start_thread_from_message", fake_start_thread)

    thread_id = await dev_gateway._discord_target_channel_id(
        settings=settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "threads.json",
        ),
        conversation_id="voice-call-123",
        surface="voice",
    )

    assert thread_id == "voice-thread-1"
    assert starter_messages == ["🎙️ Voice SkyAI разговор `voice-call-123`"]
    assert created_names == ["🎙️ Voice SkyAI · voice-call-123"]


@pytest.mark.asyncio
async def test_mirror_voice_to_discord_skips_when_disabled(tmp_path: Path) -> None:
    result = await dev_gateway.mirror_voice_to_discord(
        {"call_id": "call-voice", "transcript": "Здравейте"},
        {
            "status": "ok",
            "version": "v-test",
            "call_id": "call-voice",
            "conversation_id": "voice-call",
            "action": "speak",
            "spoken_reply": "Здравейте.",
            "display_reply": "Здравейте.",
            "trace": {},
        },
        settings(tmp_path),
        stage="turn",
    )

    assert result == {"status": "skipped", "reason": "disabled"}


@pytest.mark.asyncio
async def test_mirror_voice_to_discord_posts_to_configured_channel(tmp_path: Path, monkeypatch) -> None:
    posted: list[tuple[str, str]] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        posted.append((channel_id, content))
        return {
            "id": (
                "voice-starter-1"
                if content.startswith("🎙️ Voice SkyAI разговор `")
                else "voice-message-1"
            )
        }

    def fake_start_thread(
        channel_id: str,
        message_id: str,
        token: str,
        name: str,
    ) -> dict[str, str]:
        assert channel_id == dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID
        assert message_id == "voice-starter-1"
        return {"id": "voice-thread-1"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(
        dev_gateway,
        "_discord_start_thread_from_message",
        fake_start_thread,
    )

    result = await dev_gateway.mirror_voice_to_discord(
        {
            "call_id": "call-voice",
            "conversation_id": "voice-call",
            "transcript": "Имате ли свободни часове?",
            "pbx_extension": "399",
        },
        {
            "status": "ok",
            "version": "v-test",
            "call_id": "call-voice",
            "conversation_id": "voice-call",
            "action": "speak",
            "spoken_reply": "Да, проверявам свободните часове.",
            "display_reply": "Да, проверявам свободните часове.",
            "trace": {"raw_audio_stored": False},
        },
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "voice-threads.json",
        ),
        stage="turn",
    )

    assert result == {
        "status": "posted",
        "channel_id": "1510888721614901358",
        "target_channel_id": "voice-thread-1",
        "message_id": "voice-message-1",
        "message_ids": ["voice-message-1"],
        "message_count": 1,
    }
    assert posted[0][0] == "1510888721614901358"
    assert posted[0][1].startswith("🎙️ Voice SkyAI разговор `")
    assert posted[1][0] == "voice-thread-1"
    assert posted[1][1].startswith("**🎙️ Voice SkyAI разговор**")


@pytest.mark.asyncio
async def test_enabled_runtime_mirrors_every_payload_to_exact_configured_channel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    posted_channels: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        posted_channels.append(channel_id)
        return {
            "id": (
                "starter-1"
                if content.startswith("SkyAI v2 разговор `")
                else "message-1"
            )
        }

    def fake_start_thread(
        channel_id: str,
        message_id: str,
        token: str,
        name: str,
    ) -> dict[str, str]:
        assert channel_id == dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID
        assert message_id == "starter-1"
        return {"id": "thread-1"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(
        dev_gateway,
        "_discord_start_thread_from_message",
        fake_start_thread,
    )

    result = await dev_gateway.mirror_to_discord(
        {
            "conversation_id": "test-qa-smoke-preview-dev",
            "message": "SMOKE test preview dev",
            "origin_class": "test",
            "is_test": True,
            "_server_test_signal": "synthetic_smoke",
            "_server_request_provenance": {"origin": "https://evil.example"},
            "metadata": {
                "page_referrer": "http://localhost/?skyai_test=1",
                "client_ip": "127.0.0.1",
            },
        },
        {
            "status": "ok",
            "conversation_id": "test-qa-smoke-preview-dev",
            "reply": "OK",
            "trace": {},
        },
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "threads.json",
        ),
    )

    assert result == {
        "status": "posted",
        "channel_id": "1510888721614901358",
        "target_channel_id": "thread-1",
        "message_id": "message-1",
        "message_ids": ["message-1"],
        "message_count": 1,
    }
    assert posted_channels == ["1510888721614901358", "thread-1"]


@pytest.mark.asyncio
async def test_missing_external_id_fails_closed_without_discord_posts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    posted: list[tuple[str, str]] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        posted.append((channel_id, content))
        return {"id": f"message-{len(posted)}"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    request_payload = {"message": "Здравей"}

    result = await dev_gateway.mirror_to_discord(
        request_payload,
        {"status": "ok", "reply": "Здравей", "trace": {}},
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "threads.json",
        ),
    )

    assert result == {
        "status": "error",
        "reason": "conversation_id must be a nonempty string",
    }
    assert request_payload == {"message": "Здравей"}
    assert posted == []


@pytest.mark.asyncio
async def test_missing_voice_conversation_id_fails_closed_without_discord_posts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    generated_calls = 0
    posted: list[tuple[str, str]] = []

    def fake_voice_call_id(payload: dict) -> str:
        nonlocal generated_calls
        value = payload.get("call_id")
        if isinstance(value, str) and value:
            return value
        generated_calls += 1
        return f"generated-call-{generated_calls}"

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        posted.append((channel_id, content))
        return {"id": f"message-{len(posted)}"}

    monkeypatch.setattr(
        dev_gateway,
        "voice_call_id_from_payload",
        fake_voice_call_id,
    )
    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    request_payload = {"transcript": "Здравей"}

    result = await dev_gateway.mirror_voice_to_discord(
        request_payload,
        {
            "status": "ok",
            "action": "speak",
            "spoken_reply": "Здравей",
            "trace": {},
        },
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id=dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "voice-threads.json",
        ),
        stage="turn",
    )

    assert result == {
        "status": "error",
        "reason": "conversation_id must be a nonempty string",
    }
    assert generated_calls == 0
    assert request_payload == {"transcript": "Здравей"}
    assert posted == []


@pytest.mark.asyncio
async def test_enabled_mirror_rejects_any_noncanonical_runtime_channel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    posted_channels: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        posted_channels.append(channel_id)
        return {
            "id": (
                "starter-1"
                if content.startswith("SkyAI v2 разговор `")
                else "message-1"
            )
        }

    def fake_start_thread(
        channel_id: str,
        message_id: str,
        token: str,
        name: str,
    ) -> dict[str, str]:
        return {"id": "thread-1"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(
        dev_gateway,
        "_discord_start_thread_from_message",
        fake_start_thread,
    )

    result = await dev_gateway.mirror_to_discord(
        {
            "conversation_id": "same-payload",
            "message": "SMOKE",
            "origin_class": "test",
        },
        {
            "status": "ok",
            "conversation_id": "same-payload",
            "reply": "OK",
            "trace": {},
        },
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="separate-smoke-runtime-channel",
            discord_mirror_create_threads=True,
        ),
    )

    assert result["status"] == "error"
    assert dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID in result["reason"]
    assert posted_channels == []


@pytest.mark.asyncio
async def test_exact_conversation_id_creates_once_and_reuses_one_thread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    conversation_id = " \tQA:smoke/ß/🧪\n"
    starter_posts: list[tuple[str, str]] = []
    thread_posts: list[tuple[str, str]] = []
    started_threads: list[tuple[str, str, str]] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        if content.startswith("SkyAI v2 разговор"):
            starter_posts.append((channel_id, content))
            return {"id": "starter-1"}
        thread_posts.append((channel_id, content))
        return {"id": f"turn-{len(thread_posts)}"}

    def fake_start_thread(
        channel_id: str,
        message_id: str,
        token: str,
        name: str,
    ) -> dict[str, str]:
        started_threads.append((channel_id, message_id, name))
        return {"id": "thread-1"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(
        dev_gateway,
        "_discord_start_thread_from_message",
        fake_start_thread,
    )
    store_path = tmp_path / "threads.json"
    mirror_settings = settings(
        tmp_path,
        discord_mirror_enabled=True,
        discord_mirror_bot_token="token",
        discord_mirror_channel_id="1510888721614901358",
        discord_mirror_create_threads=True,
        discord_mirror_thread_store=store_path,
    )
    payload = {"conversation_id": conversation_id, "message": "Здравей"}
    response = {
        "status": "ok",
        "conversation_id": conversation_id,
        "reply": "Здравей",
        "trace": {},
    }

    first = await dev_gateway.mirror_to_discord(payload, response, mirror_settings)
    second = await dev_gateway.mirror_to_discord(payload, response, mirror_settings)

    mapping = json.loads(store_path.read_text(encoding="utf-8"))
    exact_key = f"chat:1510888721614901358:{conversation_id}"
    assert mapping[exact_key] == "thread-1"
    assert starter_posts == [
        (
            "1510888721614901358",
            f"SkyAI v2 разговор `{conversation_id}`",
        )
    ]
    assert started_threads == [
        ("1510888721614901358", "starter-1", f"SkyAI v2 · {conversation_id[:36]}")
    ]
    assert [channel_id for channel_id, _content in thread_posts] == [
        "thread-1",
        "thread-1",
    ]
    assert all(
        f"**SkyAI v2 canary · {conversation_id}**" in content
        for _channel_id, content in thread_posts
    )
    assert first["target_channel_id"] == "thread-1"
    assert second["target_channel_id"] == "thread-1"


@pytest.mark.asyncio
async def test_whitespace_case_and_unicode_lookalike_ids_get_distinct_threads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    conversation_ids = ["Case", "case", " Case", "Сase"]
    starter_count = 0
    thread_count = 0

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        nonlocal starter_count
        if content.startswith("SkyAI v2 разговор `"):
            starter_count += 1
            return {"id": f"starter-{starter_count}"}
        return {"id": f"turn-{channel_id}"}

    def fake_start_thread(
        channel_id: str,
        message_id: str,
        token: str,
        name: str,
    ) -> dict[str, str]:
        nonlocal thread_count
        thread_count += 1
        return {"id": f"thread-{thread_count}"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(
        dev_gateway,
        "_discord_start_thread_from_message",
        fake_start_thread,
    )
    store_path = tmp_path / "threads.json"
    mirror_settings = settings(
        tmp_path,
        discord_mirror_enabled=True,
        discord_mirror_bot_token="token",
        discord_mirror_channel_id=dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
        discord_mirror_create_threads=True,
        discord_mirror_thread_store=store_path,
    )

    results = []
    for conversation_id in conversation_ids:
        results.append(
            await dev_gateway.mirror_to_discord(
                {"conversation_id": conversation_id, "message": "exact"},
                {
                    "status": "ok",
                    "conversation_id": conversation_id,
                    "reply": "exact",
                    "trace": {},
                },
                mirror_settings,
            )
        )

    mapping = json.loads(store_path.read_text(encoding="utf-8"))
    assert starter_count == 4
    assert thread_count == 4
    assert {result["target_channel_id"] for result in results} == {
        "thread-1",
        "thread-2",
        "thread-3",
        "thread-4",
    }
    assert set(mapping) == {
        (
            f"chat:{dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID}:"
            f"{conversation_id}"
        )
        for conversation_id in conversation_ids
    }


@pytest.mark.asyncio
async def test_legacy_mapping_key_is_not_reused_without_configured_channel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    conversation_id = "legacy-conversation"
    store_path = tmp_path / "threads.json"
    store_path.write_text(
        json.dumps({conversation_id: "legacy-thread"}),
        encoding="utf-8",
    )
    starters: list[tuple[str, str]] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        starters.append((channel_id, content))
        return {"id": "new-starter"}

    def fake_start_thread(
        channel_id: str,
        message_id: str,
        token: str,
        name: str,
    ) -> dict[str, str]:
        assert message_id == "new-starter"
        return {"id": "new-thread"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(
        dev_gateway,
        "_discord_start_thread_from_message",
        fake_start_thread,
    )
    mirror_settings = settings(
        tmp_path,
        discord_mirror_enabled=True,
        discord_mirror_bot_token="token",
        discord_mirror_channel_id=dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
        discord_mirror_create_threads=True,
        discord_mirror_thread_store=store_path,
    )

    target = await dev_gateway._discord_target_channel_id(
        settings=mirror_settings,
        conversation_id=conversation_id,
    )

    mapping = json.loads(store_path.read_text(encoding="utf-8"))
    exact_key = (
        f"chat:{dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID}:{conversation_id}"
    )
    assert target == "new-thread"
    assert starters == [
        (
            dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
            f"SkyAI v2 разговор `{conversation_id}`",
        )
    ]
    assert mapping[conversation_id] == "legacy-thread"
    assert mapping[exact_key] == "new-thread"


@pytest.mark.asyncio
async def test_concurrent_first_turns_create_exactly_one_thread_and_reuse_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    starter_posts: list[str] = []
    thread_posts: list[str] = []
    start_calls: list[tuple[str, str]] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        if content.startswith("SkyAI v2 разговор"):
            starter_posts.append(content)
            time.sleep(0.03)
            return {"id": "starter-1"}
        thread_posts.append(channel_id)
        return {"id": f"turn-{len(thread_posts)}"}

    def fake_start_thread(
        channel_id: str,
        message_id: str,
        token: str,
        name: str,
    ) -> dict[str, str]:
        start_calls.append((channel_id, message_id))
        return {"id": "thread-1"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(
        dev_gateway,
        "_discord_start_thread_from_message",
        fake_start_thread,
    )
    store_path = tmp_path / "threads.json"
    mirror_settings = settings(
        tmp_path,
        discord_mirror_enabled=True,
        discord_mirror_bot_token="token",
        discord_mirror_channel_id="1510888721614901358",
        discord_mirror_create_threads=True,
        discord_mirror_thread_store=store_path,
    )
    payload = {"conversation_id": "concurrent-conversation", "message": "Здравей"}
    response = {
        "status": "ok",
        "conversation_id": "concurrent-conversation",
        "reply": "Здравей",
        "trace": {},
    }

    results = await asyncio.gather(
        *[
            dev_gateway.mirror_to_discord(payload, response, mirror_settings)
            for _ in range(12)
        ]
    )

    assert len(starter_posts) == 1
    assert start_calls == [("1510888721614901358", "starter-1")]
    assert thread_posts == ["thread-1"] * 12
    assert {result["target_channel_id"] for result in results} == {"thread-1"}
    assert json.loads(store_path.read_text(encoding="utf-8"))[
        "chat:1510888721614901358:concurrent-conversation"
    ] == "thread-1"


@pytest.mark.skipif(dev_gateway.fcntl is None, reason="POSIX flock verification")
def test_thread_mapping_lock_excludes_another_process(tmp_path: Path) -> None:
    store_path = tmp_path / "threads.json"
    lock_path = store_path.with_suffix(store_path.suffix + ".lock")
    probe = (
        "import fcntl, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "path.parent.mkdir(parents=True, exist_ok=True)\n"
        "with open(path, 'a+b') as handle:\n"
        "    try:\n"
        "        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    except BlockingIOError:\n"
        "        raise SystemExit(0)\n"
        "    raise SystemExit(7)\n"
    )

    with dev_gateway._thread_mapping_file_lock(store_path):
        completed = subprocess.run(
            [sys.executable, "-c", probe, str(lock_path)],
            check=False,
            capture_output=True,
            text=True,
        )

    assert completed.returncode == 0, completed.stderr


def test_atomic_mapping_write_preserves_previous_file_if_replace_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store_path = tmp_path / "threads.json"
    previous = {"chat:channel:conversation": "thread-old"}
    store_path.write_text(json.dumps(previous), encoding="utf-8")

    def fail_replace(_temporary_path, _target_path):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(utils, "atomic_replace", fail_replace)

    with pytest.raises(OSError, match="simulated crash"):
        dev_gateway._write_thread_mapping(
            store_path,
            {"chat:channel:conversation": "thread-new"},
        )

    assert json.loads(store_path.read_text(encoding="utf-8")) == previous
    assert list(tmp_path.glob(".threads_*.tmp")) == []


def test_atomic_mapping_write_round_trips_exact_unicode_key(tmp_path: Path) -> None:
    store_path = tmp_path / "threads.json"
    conversation_id = " \tQA:smoke/ß/🧪\n"
    mapping = {
        f"chat:1510888721614901358:{conversation_id}": "thread-1",
    }

    dev_gateway._write_thread_mapping(store_path, mapping)

    assert dev_gateway._load_thread_mapping(store_path) == mapping
    assert mapping.keys() == json.loads(store_path.read_text(encoding="utf-8")).keys()


@pytest.mark.asyncio
async def test_response_conversation_id_disagreement_fails_closed_without_posting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def forbidden_post(*_args, **_kwargs):
        raise AssertionError("a mismatched response must not reach Discord")

    monkeypatch.setattr(dev_gateway, "_discord_post_message", forbidden_post)

    result = await dev_gateway.mirror_to_discord(
        {"conversation_id": "request-id", "message": "Здравей"},
        {
            "status": "ok",
            "conversation_id": "response-id",
            "reply": "Здравей",
            "trace": {},
        },
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id=dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
            discord_mirror_create_threads=True,
        ),
    )

    assert result["status"] == "error"
    assert "does not exactly match request" in result["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_id", [None, "", 7, ["message-id"]])
async def test_malformed_discord_starter_message_id_fails_closed(
    tmp_path: Path,
    monkeypatch,
    invalid_id,
) -> None:
    thread_calls: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict:
        return {"id": invalid_id}

    def fake_start_thread(
        channel_id: str,
        message_id: str,
        token: str,
        name: str,
    ) -> dict[str, str]:
        thread_calls.append(message_id)
        return {"id": "must-not-be-used"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(
        dev_gateway,
        "_discord_start_thread_from_message",
        fake_start_thread,
    )

    result = await dev_gateway.mirror_to_discord(
        {"conversation_id": "conversation", "message": "Здравей"},
        {
            "status": "ok",
            "conversation_id": "conversation",
            "reply": "Здравей",
            "trace": {},
        },
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id=dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "starter-threads.json",
        ),
    )

    assert result["status"] == "error"
    assert result["reason"] == (
        "Discord message response must contain a nonempty string id"
    )
    assert thread_calls == []


@pytest.mark.asyncio
async def test_malformed_discord_thread_id_fails_closed_without_base_channel_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    posted_channels: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        posted_channels.append(channel_id)
        return {"id": "starter-1"}

    def fake_start_thread(
        channel_id: str,
        message_id: str,
        token: str,
        name: str,
    ) -> dict[str, int]:
        return {"id": 123}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(
        dev_gateway,
        "_discord_start_thread_from_message",
        fake_start_thread,
    )

    result = await dev_gateway.mirror_to_discord(
        {"conversation_id": "conversation", "message": "Здравей"},
        {
            "status": "ok",
            "conversation_id": "conversation",
            "reply": "Здравей",
            "trace": {},
        },
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id=dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "thread-threads.json",
        ),
    )

    assert result["status"] == "error"
    assert result["reason"] == (
        "Discord thread response must contain a nonempty string id"
    )
    assert posted_channels == [dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID]


@pytest.mark.asyncio
async def test_malformed_discord_turn_message_id_is_not_reported_as_posted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    post_count = 0

    def fake_post_message(channel_id: str, token: str, content: str) -> dict:
        nonlocal post_count
        post_count += 1
        if post_count == 1:
            return {"id": "starter-1"}
        return {"id": 456}

    def fake_start_thread(
        channel_id: str,
        message_id: str,
        token: str,
        name: str,
    ) -> dict[str, str]:
        return {"id": "thread-1"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(
        dev_gateway,
        "_discord_start_thread_from_message",
        fake_start_thread,
    )

    result = await dev_gateway.mirror_to_discord(
        {"conversation_id": "conversation", "message": "Здравей"},
        {
            "status": "ok",
            "conversation_id": "conversation",
            "reply": "Здравей",
            "trace": {},
        },
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id=dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "turn-threads.json",
        ),
    )

    assert result["status"] == "error"
    assert result["reason"] == (
        "Discord message response must contain a nonempty string id"
    )
    assert post_count == 2


@pytest.mark.asyncio
async def test_corrupt_mapping_fails_closed_without_creating_duplicate_thread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store_path = tmp_path / "threads.json"
    store_path.write_text('{"truncated":', encoding="utf-8")
    starter_posts: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        starter_posts.append(content)
        return {"id": "must-not-be-created"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)

    result = await dev_gateway.mirror_to_discord(
        {"conversation_id": "existing-unknown-thread", "message": "Здравей"},
        {
            "status": "ok",
            "conversation_id": "existing-unknown-thread",
            "reply": "Здравей",
            "trace": {},
        },
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=store_path,
        ),
    )

    assert result["status"] == "error"
    assert "invalid JSON" in result["reason"]
    assert starter_posts == []


@pytest.mark.asyncio
async def test_runtime_configuration_not_request_provenance_is_the_mirror_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    posted_channels: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        posted_channels.append(channel_id)
        return {
            "id": (
                "starter-1"
                if content.startswith("SkyAI v2 разговор `")
                else "message-1"
            )
        }

    def fake_start_thread(
        channel_id: str,
        message_id: str,
        token: str,
        name: str,
    ) -> dict[str, str]:
        assert channel_id == dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID
        assert message_id == "starter-1"
        return {"id": "thread-1"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(
        dev_gateway,
        "_discord_start_thread_from_message",
        fake_start_thread,
    )

    payload = {
        "conversation_id": "synthetic-prod-origin",
        "message": "SMOKE",
        "_server_request_provenance": {
            "origin": "https://skyvision.bg",
            "referer": "https://skyvision.bg/",
        },
        "_server_test_signal": "synthetic_smoke",
    }
    response = {
        "status": "ok",
        "conversation_id": "synthetic-prod-origin",
        "reply": "OK",
        "trace": {},
    }
    disabled = await dev_gateway.mirror_to_discord(
        payload,
        response,
        settings(
            tmp_path,
            discord_mirror_enabled=False,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
        ),
    )
    enabled = await dev_gateway.mirror_to_discord(
        payload,
        response,
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "threads.json",
        ),
    )

    assert disabled == {"status": "skipped", "reason": "disabled"}
    assert enabled["status"] == "posted"
    assert posted_channels == ["1510888721614901358", "thread-1"]


@pytest.mark.asyncio
async def test_build_compare_response_runs_dev_and_prod_sides(tmp_path: Path) -> None:
    async def fake_runner(message, history, conversation_id, canary_settings):
        return {"final_response": f"DEV: {message}"}

    def fake_prod_caller(payload, canary_settings):
        return {
            "status": "ok",
            "version": "prod-v",
            "reply": f"PROD: {payload['message']}",
            "cards": [{"title": "card"}],
            "trace": {"model": "gpt-5.6-sol", "latency_ms": 20},
        }

    response = await dev_gateway.build_compare_response(
        {"conversation_id": "c1", "message": "Има ли масаж?"},
        settings(tmp_path, compare_prod_base_url="https://prod.example"),
        agent_runner=fake_runner,
        prod_caller=fake_prod_caller,
    )

    assert response["status"] == "ok"
    assert response["dev_v2"]["reply"] == "DEV: Има ли масаж?"
    assert response["prod_current"]["reply"] == "PROD: Има ли масаж?"
    assert response["prod_current"]["cards_count"] == 1
    assert response["cards_compare"]["prod_count"] == 1


def test_compare_side_preserves_distinct_prose_fields_without_fallback_selection() -> None:
    compact = dev_gateway._compact_compare_side(
        {
            "status": "error",
            "reply": "",
            "reason": " exact reason ",
            "error": "exact_error_code",
            "cards": [],
        }
    )

    assert compact["reply"] == ""
    assert compact["reason"] == " exact reason "
    assert compact["error"] == "exact_error_code"


@pytest.mark.asyncio
async def test_build_compare_response_compares_card_links_prices_and_images(
    tmp_path: Path,
) -> None:
    async def fake_runner(message, history, conversation_id, canary_settings):
        return {
            "final_response": "Бих предложил този масаж.",
            "cards": [
                {
                    "title": "Масаж за двама",
                    "url": "https://skyvision.bg/подарък/масаж/масаж-за-двама/",
                    "price_eur": "90.00",
                    "location": "София",
                    "image": "https://cdn.example/massage.jpg",
                }
            ],
        }

    def fake_prod_caller(payload, canary_settings):
        return {
            "status": "ok",
            "version": "prod-v",
            "reply": "PROD reply",
            "cards": [
                {
                    "title": "Масаж за двама",
                    "url": "https://skyvision.bg/подарък/масаж/масаж-за-двама/",
                    "price_eur": "90.00",
                    "image": "https://cdn.example/massage.jpg",
                }
            ],
            "trace": {"model": "gpt-5.6-sol"},
        }

    response = await dev_gateway.build_compare_response(
        {"conversation_id": "c1", "message": "Има ли масаж?"},
        settings(tmp_path, compare_prod_base_url="https://prod.example"),
        agent_runner=fake_runner,
        prod_caller=fake_prod_caller,
    )

    assert response["dev_v2"]["cards"] == [
        {
            "title": "Масаж за двама",
            "url": "https://skyvision.bg/подарък/масаж/масаж-за-двама/",
            "price_eur": "90.00",
            "location": "София",
            "image": "https://cdn.example/massage.jpg",
        }
    ]
    assert response["cards_compare"]["shared_urls"] == [
        "https://skyvision.bg/подарък/масаж/масаж-за-двама/"
    ]
    assert response["cards_compare"]["shared_titles"] == ["Масаж за двама"]
    assert response["cards_compare"]["dev_missing_price_count"] == 0
    assert response["cards_compare"]["prod_missing_image_count"] == 0


@pytest.mark.asyncio
async def test_build_compare_response_requires_prod_base_url(tmp_path: Path) -> None:
    response = await dev_gateway.build_compare_response(
        {"conversation_id": "c1", "message": "Здравей"},
        settings(tmp_path),
    )

    assert response["status"] == "error"
    assert response["error"] == "compare_prod_not_configured"
