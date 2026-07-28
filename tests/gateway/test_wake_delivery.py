"""Tests for gateway/wake.py — background wake delivery.

Two strategies:
* push-capable adapters keep the synthetic MessageEvent / handle_message path;
* the stateless API server (supports_async_delivery=False) self-POSTs
  /v1/chat/completions with the RAW session id in X-Hermes-Session-Id, so the
  wake turn resumes the REAL session instead of a parallel invisible one
  keyed by build_session_key().
"""

import asyncio
import copy
import json
import shutil

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.wake import deliver_wake, adapter_supports_push

_TEST_API_PROFILE_IDENTITY = None


def _runtime_effect(
    *,
    authority="conversation-root-wake-test",
    baseline=17,
):
    return {
        "schema": "hermes.runtime-effect.v1",
        "kind": "isolated_workspace_may_have_changed.v1",
        "workspace_lease_authority": authority,
        "baseline_edit_generation": baseline,
    }


def _api_execution_context():
    from gateway.api_execution_context import transport_semantic_digest

    route_digest = transport_semantic_digest(
        model="openai/gpt-5",
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_mode="",
    )
    effective_digest = transport_semantic_digest(
        model="openai/gpt-5",
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_mode="chat_completions",
    )
    return {
        "schema": "hermes.api-detached-execution-context.v1",
        "gateway_session_key": "memory:stable:customer-42",
        "request_model": "alias-42",
        "request_provider": "",
        "model_options": {
            "reasoning": {"enabled": True, "effort": "high"},
            "service_tier": "priority",
        },
        "route_alias": "alias-42",
        "route_model": "openai/gpt-5",
        "route_provider": "openai",
        "route_semantic_sha256": route_digest,
        "session_model": "",
        "confirmed_runtime_lock": False,
        "requested_runtime": {
            "model": "alias-42",
            "provider": "",
        },
        "route_source": "model_routes",
        "effective_model": "openai/gpt-5",
        "effective_provider": "openai",
        "effective_transport_sha256": effective_digest,
    }


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda context: context.__setitem__(
                "confirmed_runtime_lock",
                1,
            ),
            "confirmed_runtime_lock must be a boolean",
        ),
        (
            lambda context: context.__setitem__(
                "route_source",
                "caller_claimed",
            ),
            "route_source is unsupported",
        ),
        (
            lambda context: context["model_options"]["reasoning"].__setitem__(
                "effort",
                "extreme",
            ),
            "reasoning effort is unsupported",
        ),
        (
            lambda context: context.__setitem__("effective_model", ""),
            "effective_model is required",
        ),
        (
            lambda context: context.__setitem__("api_key", "must-not-persist"),
            "unsupported fields",
        ),
    ],
)
def test_api_execution_context_rejects_unsafe_or_ambiguous_values(
    mutate,
    error,
):
    from gateway.api_execution_context import (
        ApiExecutionContextError,
        normalize_api_execution_context,
    )

    context = copy.deepcopy(_api_execution_context())
    mutate(context)
    with pytest.raises(ApiExecutionContextError, match=error):
        normalize_api_execution_context(context, allow_none=False)


def test_transport_digest_canonicalizes_endpoint_without_persisting_it():
    from gateway.api_execution_context import transport_semantic_digest

    first = transport_semantic_digest(
        model="openai/gpt-5",
        provider="openai",
        base_url="https://API.EXAMPLE:443/v1/",
        api_mode="CHAT_COMPLETIONS",
    )
    second = transport_semantic_digest(
        model="openai/gpt-5",
        provider="openai",
        base_url="https://api.example/v1",
        api_mode="chat_completions",
    )

    assert first == second
    assert "api.example" not in first


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:secret@api.example/v1",
        "https://api.example/v1?tenant=secret",
        "https://api.example/v1#fragment",
        "file:///tmp/provider.sock",
    ],
)
def test_transport_digest_rejects_request_local_or_non_http_endpoint(base_url):
    from gateway.api_execution_context import (
        ApiExecutionContextError,
        transport_semantic_digest,
    )

    with pytest.raises(ApiExecutionContextError):
        transport_semantic_digest(
            model="openai/gpt-5",
            provider="openai",
            base_url=base_url,
            api_mode="chat_completions",
        )


def _wake_idempotency_key(
    *,
    session_id,
    text,
    effect,
    producer_id="deleg-runtime-effect-test",
    profile=None,
    delivery_home=None,
    profile_generation=None,
):
    import gateway.wake as wake_mod

    return wake_mod._internal_wake_idempotency_key(
        producer_id=producer_id,
        session_id=session_id,
        text=text,
        runtime_effect=effect,
        profile=profile,
        delivery_home=delivery_home,
        profile_generation=profile_generation,
    )


@pytest.fixture(autouse=True)
def _clear_internal_wake_tokens(tmp_path):
    import gateway.wake as wake_mod
    from gateway.api_request_scope import capture_api_profile_identity

    global _TEST_API_PROFILE_IDENTITY
    profile_home = tmp_path / "frozen-api-profile"
    profile_home.mkdir()
    _TEST_API_PROFILE_IDENTITY = capture_api_profile_identity(
        "default",
        profile_home,
    )

    with wake_mod._WAKE_TOKEN_LOCK:
        wake_mod._WAKE_TOKENS.clear()
    yield
    with wake_mod._WAKE_TOKEN_LOCK:
        wake_mod._WAKE_TOKENS.clear()


class PushAdapter:
    """Default adapter shape — no supports_async_delivery attribute."""

    def __init__(self):
        self.handled = []

    async def handle_message(self, event):
        self.handled.append(event)


class _LiveSessionDB:
    def get_session(self, session_id):
        return {
            "id": session_id,
            "ended_at": None,
            "end_reason": None,
        }

    def get_compression_tip(self, session_id):
        return session_id

    def get_conversation_root(self, session_id):
        return "conversation-root-wake-test"


class _RecordingSessionDB:
    def __init__(self, rows, *, tips=None, authorities=None):
        self.rows = rows
        self.tips = tips or {}
        self.authorities = authorities or {}
        self.calls = []

    def get_session(self, session_id):
        self.calls.append(("get_session", session_id))
        return self.rows.get(session_id)

    def get_compression_tip(self, session_id):
        self.calls.append(("get_compression_tip", session_id))
        return self.tips.get(session_id)

    def get_conversation_root(self, session_id):
        self.calls.append(("get_conversation_root", session_id))
        return self.authorities.get(session_id)


class ApiServerLikeAdapter:
    supports_async_delivery = False

    def __init__(
        self,
        host="0.0.0.0",
        port=0,
        key="test-key",
        model="hermes",
        session_db=None,
        profile_identity=None,
    ):
        self._host = host
        self._port = port
        self._api_key = key
        self._model_name = model
        self._session_db = session_db or _LiveSessionDB()
        identity = profile_identity or _TEST_API_PROFILE_IDENTITY
        self._api_profile_inventory = (identity,)

    async def _ensure_session_db_async(self):
        return self._session_db

    def _freeze_api_profile_inventory(self):
        return self._api_profile_inventory

    async def handle_message(self, event):  # pragma: no cover — must NOT be hit
        raise AssertionError("non-push adapter must not receive handle_message wakes")


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="group",
    )


def test_adapter_supports_push_default_true():
    assert adapter_supports_push(PushAdapter()) is True
    assert adapter_supports_push(ApiServerLikeAdapter()) is False


def test_deliver_wake_push_adapter_uses_handle_message():
    adapter = PushAdapter()
    asyncio.run(deliver_wake(adapter, text="wake up", source=_source()))
    assert len(adapter.handled) == 1
    evt = adapter.handled[0]
    assert evt.text == "wake up"
    assert evt.internal is True
    assert evt.source.chat_id == "chat-1"


def test_deliver_wake_push_does_not_consult_mutable_profile_registry(
    monkeypatch,
):
    from hermes_cli import profiles as profiles_mod

    registry_lookup = pytest.fail
    monkeypatch.setattr(
        profiles_mod,
        "profile_exists",
        lambda *_args, **_kwargs: registry_lookup(
            "push wake consulted mutable profile registry"
        ),
    )
    adapter = PushAdapter()

    asyncio.run(
        deliver_wake(
            adapter,
            text="wake up",
            source=_source(),
            profile="alpha",
        )
    )

    assert len(adapter.handled) == 1


def test_deliver_wake_push_adapter_requires_source():
    with pytest.raises(ValueError):
        asyncio.run(deliver_wake(PushAdapter(), text="x", session_id="sid"))


def test_deliver_wake_non_push_requires_session_id():
    with pytest.raises(ValueError):
        asyncio.run(deliver_wake(ApiServerLikeAdapter(), text="x", source=_source()))


def test_deliver_wake_non_push_requires_api_key():
    """Session continuation is 403-gated on API_SERVER_KEY — a missing key
    must fail loudly instead of running the wake in a fresh session."""
    adapter = ApiServerLikeAdapter(key="")
    with pytest.raises(RuntimeError, match="API_SERVER_KEY"):
        asyncio.run(deliver_wake(adapter, text="x", session_id="raw-sid"))


async def _serve(handler, *, path="/v1/chat/completions"):
    """Spin an in-process aiohttp server on an ephemeral loopback port."""
    from aiohttp import web

    app = web.Application()
    app.router.add_post(path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


def test_deliver_wake_non_push_self_posts_raw_session_id(monkeypatch):
    """The self-post carries the RAW session id header + bearer auth and a
    single user message with stream=false — the exact entry point real
    gateway turns use."""
    from aiohttp import web

    seen = {}

    async def handler(request):
        seen["session_id"] = request.headers.get("X-Hermes-Session-Id")
        seen["session_key"] = request.headers.get("X-Hermes-Session-Key")
        seen["idempotency_key"] = request.headers.get("Idempotency-Key")
        seen["wake_token"] = request.headers.get(
            "X-Hermes-Internal-Wake-Token"
        )
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = await request.json()
        return web.json_response({"choices": [{"message": {"content": "ok"}}]})

    async def run():
        runner, port = await _serve(handler)
        try:
            adapter = ApiServerLikeAdapter(host="0.0.0.0", port=port, key="sekrit")
            await deliver_wake(
                adapter,
                text="task done — wake",
                session_id="raw-sid-42",
                producer_id="deleg-stable-42",
                execution_context=_api_execution_context(),
            )
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert seen["session_id"] == "raw-sid-42"
    assert seen["session_key"] == "memory:stable:customer-42"
    assert seen["idempotency_key"].startswith("hermes-internal-wake-v1-")
    assert seen["wake_token"]
    assert seen["auth"] == "Bearer sekrit"
    assert seen["body"]["stream"] is False
    assert seen["body"]["model"] == "alias-42"
    assert seen["body"]["model_options"] == {
        "reasoning": {"enabled": True, "effort": "high"},
        "service_tier": "priority",
    }
    assert seen["body"]["messages"] == [
        {"role": "user", "content": "task done — wake"}
    ]


def test_deliver_wake_without_effect_follows_live_compression_tip(monkeypatch):
    """Ordinary async completions resume the live tip, not the ended parent."""

    import gateway.wake as wake_mod

    db = _RecordingSessionDB(
        {
            "parent": {
                "id": "parent",
                "ended_at": "2026-07-28T09:00:00",
                "end_reason": "compression",
            },
            "tip": {
                "id": "tip",
                "ended_at": None,
                "end_reason": None,
            },
        },
        tips={"parent": "tip"},
    )
    posted = {}

    async def _capture_post(_adapter, **kwargs):
        posted.update(kwargs)

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", _capture_post)
    asyncio.run(
        deliver_wake(
            ApiServerLikeAdapter(session_db=db),
            text="ordinary completion",
            session_id="parent",
        )
    )

    assert posted["session_id"] == "tip"
    assert posted["runtime_effect"] is None
    assert db.calls == [
        ("get_session", "parent"),
        ("get_compression_tip", "parent"),
        ("get_session", "tip"),
    ]


def test_deliver_wake_without_effect_rejects_explicit_boundary(monkeypatch):
    import gateway.wake as wake_mod

    db = _RecordingSessionDB({
        "parent": {
            "id": "parent",
            "ended_at": "2026-07-28T09:00:00",
            "end_reason": "session_reset",
        },
    })
    posted = []

    async def _capture_post(*args, **kwargs):
        posted.append((args, kwargs))

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", _capture_post)
    with pytest.raises(RuntimeError, match="explicit conversation boundary"):
        asyncio.run(
            deliver_wake(
                ApiServerLikeAdapter(session_db=db),
                text="stale completion",
                session_id="parent",
            )
        )

    assert posted == []
    assert db.calls == [("get_session", "parent")]


def test_deliver_wake_effect_still_requires_matching_tip_authority(monkeypatch):
    import gateway.wake as wake_mod

    db = _RecordingSessionDB(
        {
            "parent": {
                "id": "parent",
                "ended_at": "2026-07-28T09:00:00",
                "end_reason": "compression",
            },
            "tip": {
                "id": "tip",
                "ended_at": None,
                "end_reason": None,
            },
        },
        tips={"parent": "tip"},
        authorities={"tip": "different-root"},
    )
    posted = []

    async def _capture_post(*args, **kwargs):
        posted.append((args, kwargs))

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", _capture_post)
    with pytest.raises(RuntimeError, match="authority does not match"):
        asyncio.run(
            deliver_wake(
                ApiServerLikeAdapter(session_db=db),
                text="isolated completion",
                session_id="parent",
                runtime_effect=_runtime_effect(
                    authority="expected-root",
                ),
                producer_id="deleg-authority",
            )
        )

    assert posted == []
    assert db.calls == [
        ("get_session", "parent"),
        ("get_compression_tip", "parent"),
        ("get_session", "tip"),
        ("get_conversation_root", "tip"),
    ]


def test_deliver_wake_named_profile_uses_native_route_in_its_process(
    tmp_path,
    monkeypatch,
):
    """A trusted named-profile wake keeps profile authority off the URL."""
    from aiohttp import web

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "profiles" / "alpha").mkdir(parents=True)
    seen = {}

    async def handler(request):
        seen["path"] = request.path
        seen["session_id"] = request.headers.get("X-Hermes-Session-Id")
        return web.json_response({"choices": []})

    async def run():
        runner, port = await _serve(handler)
        try:
            from gateway.api_request_scope import capture_api_profile_identity

            alpha_identity = capture_api_profile_identity(
                "alpha",
                tmp_path / "profiles" / "alpha",
            )
            adapter = ApiServerLikeAdapter(
                port=port,
                profile_identity=alpha_identity,
            )
            await deliver_wake(
                adapter,
                text="alpha wake",
                session_id="alpha-session",
                profile="alpha",
            )
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert seen == {
        "path": "/v1/chat/completions",
        "session_id": "alpha-session",
    }


@pytest.mark.parametrize("profile", ("../alpha", "alpha/other"))
def test_deliver_wake_rejects_invalid_or_unserved_profile(
    profile,
    tmp_path,
    monkeypatch,
):
    import gateway.wake as wake_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "profiles" / "alpha").mkdir(parents=True)

    with pytest.raises(
        wake_mod.InternalWakeTokenError,
        match="consistent frozen API profile authority",
    ):
        asyncio.run(
            deliver_wake(
                ApiServerLikeAdapter(),
                text="x",
                session_id="sid",
                profile=profile,
            )
        )


def test_deliver_wake_rejects_profile_outside_frozen_adapter_inventory():
    import gateway.wake as wake_mod

    with pytest.raises(
        wake_mod.InternalWakeTokenError,
        match="does not match",
    ):
        asyncio.run(
            deliver_wake(
                ApiServerLikeAdapter(),
                text="x",
                session_id="sid",
                profile="not-created",
            )
        )


def test_legacy_wake_rejects_missing_or_ambiguous_frozen_inventory(tmp_path):
    import gateway.wake as wake_mod
    from gateway.api_request_scope import capture_api_profile_identity

    missing = ApiServerLikeAdapter()
    missing._freeze_api_profile_inventory = None
    missing._api_profile_inventory = None
    with pytest.raises(
        wake_mod.InternalWakeTokenError,
        match="consistent frozen API profile authority",
    ):
        asyncio.run(
            deliver_wake(
                missing,
                text="x",
                session_id="sid",
            )
        )

    alpha_home = tmp_path / "alpha"
    alpha_home.mkdir()
    ambiguous = ApiServerLikeAdapter()
    ambiguous._api_profile_inventory = (
        _TEST_API_PROFILE_IDENTITY,
        capture_api_profile_identity("alpha", alpha_home),
    )
    with pytest.raises(
        wake_mod.InternalWakeTokenError,
        match="exactly one frozen API profile identity",
    ):
        asyncio.run(
            deliver_wake(
                ambiguous,
                text="x",
                session_id="sid",
            )
        )


@pytest.mark.parametrize("replacement", ("retarget", "recreate"))
def test_legacy_wake_rejects_frozen_profile_retarget_or_replacement(
    replacement,
    tmp_path,
):
    import gateway.wake as wake_mod
    from gateway.api_request_scope import capture_api_profile_identity

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    source = tmp_path / "active"
    if replacement == "retarget":
        source.symlink_to(first, target_is_directory=True)
    else:
        source.mkdir()
    identity = capture_api_profile_identity("default", source)
    adapter = ApiServerLikeAdapter(profile_identity=identity)

    if replacement == "retarget":
        source.unlink()
        source.symlink_to(second, target_is_directory=True)
    else:
        shutil.rmtree(source)
        source.mkdir()

    with pytest.raises(
        wake_mod.InternalWakeTokenError,
        match="consistent frozen API profile authority",
    ):
        asyncio.run(
            deliver_wake(
                adapter,
                text="x",
                session_id="sid",
            )
        )


def test_internal_wake_capability_is_one_use_and_bound_to_request():
    import gateway.wake as wake_mod

    effect = _runtime_effect()
    idempotency_key = _wake_idempotency_key(
        session_id="session-1",
        text="trusted wake text",
        effect=effect,
    )
    token = wake_mod.mint_internal_wake_token(
        session_id="session-1",
        text="trusted wake text",
        runtime_effect=effect,
        producer_id="deleg-runtime-effect-test",
        now=100.0,
    )

    assert wake_mod.consume_internal_wake_token(
        token,
        session_id="session-1",
        text="trusted wake text",
        idempotency_key=idempotency_key,
        now=101.0,
    ) == effect
    with pytest.raises(wake_mod.InternalWakeTokenError, match="already consumed"):
        wake_mod.consume_internal_wake_token(
            token,
            session_id="session-1",
            text="trusted wake text",
            idempotency_key=idempotency_key,
            now=101.0,
        )


def test_no_effect_wake_capability_binds_memory_context_and_is_idempotent():
    """Ordinary completions get the same capability/dedupe contract as effects."""

    import gateway.wake as wake_mod

    context = _api_execution_context()
    kwargs = {
        "producer_id": "deleg-no-effect-42",
        "session_id": "origin-session-42",
        "text": "ordinary completion",
        "execution_context": context,
    }
    idempotency_key = wake_mod._internal_wake_idempotency_key(**kwargs)
    assert idempotency_key == wake_mod._internal_wake_idempotency_key(**kwargs)
    token = wake_mod.mint_internal_wake_token(
        session_id="resolved-tip-42",
        origin_session_id="origin-session-42",
        text="ordinary completion",
        execution_context=context,
        producer_id="deleg-no-effect-42",
    )

    envelope = wake_mod.consume_internal_wake_token(
        token,
        session_id="resolved-tip-42",
        text="ordinary completion",
        idempotency_key=idempotency_key,
        gateway_session_key="memory:stable:customer-42",
        return_envelope=True,
    )
    assert envelope["runtime_effect"] is None
    assert envelope["execution_context"] == context
    assert envelope["origin_session_id"] == "origin-session-42"
    assert envelope["producer_id"] == "deleg-no-effect-42"
    assert envelope["durable_wake_required"] is False
    assert envelope["durable_delegation_id"] == ""
    assert envelope["durable_execution_owner"] == ""
    assert envelope["profile_identity"]["profile"] == "default"
    assert envelope["profile_identity"]["source_home"]
    assert envelope["profile_identity"]["canonical_home"]
    assert envelope["profile_identity"]["profile_generation"].startswith(
        "fs-v3:"
    )


def test_durable_wake_capability_requires_explicit_bound_delegation_id():
    import gateway.wake as wake_mod

    with pytest.raises(
        wake_mod.InternalWakeTokenError,
        match="flag and delegation id",
    ):
        wake_mod.mint_internal_wake_token(
            session_id="sid",
            text="completion",
            producer_id="deleg_" + "a" * 32,
            durable_wake_required=True,
        )
    with pytest.raises(
        wake_mod.InternalWakeTokenError,
        match="must match",
    ):
        wake_mod.mint_internal_wake_token(
            session_id="sid",
            text="completion",
            producer_id="deleg_" + "a" * 32,
            durable_wake_required=True,
            durable_delegation_id="deleg_" + "b" * 32,
        )


def test_internal_wake_capability_and_retry_key_are_profile_bound(tmp_path):
    import gateway.wake as wake_mod
    from gateway.api_request_scope import capture_api_profile_identity

    effect = _runtime_effect()
    kwargs = {
        "session_id": "shared-raw-session",
        "text": "trusted wake text",
        "effect": effect,
        "producer_id": "shared-delegation-id",
    }
    default_home = tmp_path / "default"
    alpha_home = tmp_path / "alpha"
    default_home.mkdir()
    alpha_home.mkdir()
    default_identity = capture_api_profile_identity("default", default_home)
    alpha_identity = capture_api_profile_identity("alpha", alpha_home)
    default_key = _wake_idempotency_key(
        **kwargs,
        delivery_home=default_identity.canonical_home,
        profile_generation=default_identity.profile_generation,
    )
    alpha_key = _wake_idempotency_key(
        **kwargs,
        profile="alpha",
        delivery_home=alpha_identity.canonical_home,
        profile_generation=alpha_identity.profile_generation,
    )
    assert default_key != alpha_key

    token = wake_mod.mint_internal_wake_token(
        session_id=kwargs["session_id"],
        text=kwargs["text"],
        runtime_effect=effect,
        producer_id=kwargs["producer_id"],
        delivery_home=default_identity.canonical_home,
        profile_generation=default_identity.profile_generation,
    )
    with pytest.raises(
        wake_mod.InternalWakeTokenError,
        match="profile mismatch",
    ):
        wake_mod.consume_internal_wake_token(
            token,
            session_id=kwargs["session_id"],
            text=kwargs["text"],
            idempotency_key=default_key,
            profile="alpha",
            canonical_home=alpha_identity.canonical_home,
            profile_generation=alpha_identity.profile_generation,
        )


def test_internal_wake_rejects_same_path_profile_recreation(tmp_path):
    import gateway.wake as wake_mod
    from gateway.api_request_scope import capture_api_profile_identity

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    old_identity = capture_api_profile_identity("default", profile_home)
    kwargs = {
        "producer_id": "deleg-profile-generation",
        "session_id": "session-profile-generation",
        "text": "generation-bound wake",
        "delivery_home": old_identity.canonical_home,
        "profile_generation": old_identity.profile_generation,
    }
    old_key = wake_mod._internal_wake_idempotency_key(**kwargs)
    token = wake_mod.mint_internal_wake_token(**kwargs)

    for child in profile_home.iterdir():
        child.unlink()
    profile_home.rmdir()
    profile_home.mkdir()
    new_identity = capture_api_profile_identity("default", profile_home)
    assert new_identity.canonical_home == old_identity.canonical_home
    assert new_identity.profile_generation != old_identity.profile_generation
    new_key = wake_mod._internal_wake_idempotency_key(
        producer_id=kwargs["producer_id"],
        session_id=kwargs["session_id"],
        text=kwargs["text"],
        delivery_home=new_identity.canonical_home,
        profile_generation=new_identity.profile_generation,
    )
    assert new_key != old_key

    with pytest.raises(
        wake_mod.InternalWakeTokenError,
        match="profile-generation mismatch",
    ):
        wake_mod.consume_internal_wake_token(
            token,
            session_id=kwargs["session_id"],
            text=kwargs["text"],
            idempotency_key=old_key,
            canonical_home=new_identity.canonical_home,
            profile_generation=new_identity.profile_generation,
        )


def test_guessed_or_mismatched_wake_capability_cannot_forge_effect():
    import gateway.wake as wake_mod

    with pytest.raises(wake_mod.InternalWakeTokenError, match="missing"):
        wake_mod.consume_internal_wake_token(
            "attacker-authored-token",
            session_id="session-1",
            text="trusted wake text",
            idempotency_key="attacker-authored-idempotency-key",
        )

    effect = _runtime_effect()
    idempotency_key = _wake_idempotency_key(
        session_id="session-1",
        text="trusted wake text",
        effect=effect,
    )
    wrong_key_token = wake_mod.mint_internal_wake_token(
        session_id="session-1",
        text="trusted wake text",
        runtime_effect=effect,
        producer_id="deleg-runtime-effect-test",
    )
    with pytest.raises(
        wake_mod.InternalWakeTokenError,
        match="idempotency mismatch",
    ):
        wake_mod.consume_internal_wake_token(
            wrong_key_token,
            session_id="session-1",
            text="trusted wake text",
            idempotency_key="attacker-authored-idempotency-key",
        )

    token = wake_mod.mint_internal_wake_token(
        session_id="session-1",
        text="trusted wake text",
        runtime_effect=effect,
        producer_id="deleg-runtime-effect-test",
    )
    with pytest.raises(wake_mod.InternalWakeTokenError, match="session mismatch"):
        wake_mod.consume_internal_wake_token(
            token,
            session_id="attacker-session",
            text="trusted wake text",
            idempotency_key=idempotency_key,
        )
    # A mismatched attempt consumes the capability atomically; it cannot be
    # replayed later with corrected request fields.
    with pytest.raises(wake_mod.InternalWakeTokenError, match="already consumed"):
        wake_mod.consume_internal_wake_token(
            token,
            session_id="session-1",
            text="trusted wake text",
            idempotency_key=idempotency_key,
        )


def test_expired_internal_wake_capability_fails_closed():
    import gateway.wake as wake_mod

    effect = _runtime_effect()
    idempotency_key = _wake_idempotency_key(
        session_id="session-1",
        text="trusted wake text",
        effect=effect,
    )
    token = wake_mod.mint_internal_wake_token(
        session_id="session-1",
        text="trusted wake text",
        runtime_effect=effect,
        producer_id="deleg-runtime-effect-test",
        now=100.0,
        ttl_seconds=1.0,
    )
    with pytest.raises(wake_mod.InternalWakeTokenError, match="expired"):
        wake_mod.consume_internal_wake_token(
            token,
            session_id="session-1",
            text="trusted wake text",
            idempotency_key=idempotency_key,
            now=101.0,
        )


def test_runtime_effect_self_post_uses_opaque_header_without_plaintext_leak():
    from aiohttp import web

    import gateway.wake as wake_mod

    seen = {}
    effect = _runtime_effect(
        authority="opaque-root-authority-never-on-wire",
        baseline=987654321,
    )

    async def handler(request):
        seen["headers"] = dict(request.headers)
        seen["body"] = await request.json()
        return web.json_response({"choices": [{"message": {"content": "ok"}}]})

    async def run():
        runner, port = await _serve(handler)
        try:
            adapter = ApiServerLikeAdapter(port=port)
            await wake_mod._self_post_chat_completion(
                adapter,
                text="trusted wake text",
                session_id="session-1",
                runtime_effect=effect,
                producer_id="deleg-1",
            )
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert wake_mod.INTERNAL_WAKE_TOKEN_HEADER in seen["headers"]
    assert seen["headers"]["Idempotency-Key"].startswith(
        "hermes-internal-wake-v1-"
    )
    assert seen["body"]["messages"] == [
        {"role": "user", "content": "trusted wake text"}
    ]
    assert "runtime_effect" not in seen["body"]
    wire = json.dumps(
        {"headers": seen["headers"], "body": seen["body"]},
        sort_keys=True,
    )
    assert "hermes.runtime-effect.v1" not in wire
    assert "isolated_workspace_may_have_changed.v1" not in wire
    assert "opaque-root-authority-never-on-wire" not in wire
    assert "baseline_edit_generation" not in wire


def test_retry_after_server_accept_reuses_stable_idempotency_key(monkeypatch):
    """A lost first response retries with a fresh token but the same turn key."""
    from aiohttp import web

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_RETRY_DELAYS_SECONDS", (0.01,))
    calls = []
    executed_keys = set()
    executions = {"count": 0}

    async def handler(request):
        await request.json()
        key = request.headers["Idempotency-Key"]
        token = request.headers[wake_mod.INTERNAL_WAKE_TOKEN_HEADER]
        calls.append((key, token))
        if key not in executed_keys:
            executed_keys.add(key)
            executions["count"] += 1
        if len(calls) == 1:
            # The server has accepted/recorded the turn, but the response is
            # lost. The client must retry without causing a second execution.
            request.transport.close()
            return web.Response(status=200)
        return web.json_response({"choices": [{"message": {"content": "ok"}}]})

    async def run():
        runner, port = await _serve(handler)
        try:
            adapter = ApiServerLikeAdapter(port=port)
            await wake_mod._self_post_chat_completion(
                adapter,
                text="trusted wake retry",
                session_id="session-retry",
                runtime_effect=_runtime_effect(),
                producer_id="deleg-retry-after-accept",
            )
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0]
    assert calls[0][1] != calls[1][1]
    assert executions["count"] == 1


def test_deliver_wake_retries_429_then_succeeds(monkeypatch):
    """HTTP 429 (max_concurrent_runs cap) is transient — retried with backoff."""
    from aiohttp import web

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_RETRY_DELAYS_SECONDS", (0.01, 0.01, 0.01))
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return web.json_response({"error": "busy"}, status=429)
        return web.json_response({"choices": []})

    async def run():
        runner, port = await _serve(handler)
        try:
            adapter = ApiServerLikeAdapter(port=port)
            await deliver_wake(adapter, text="x", session_id="sid")
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert calls["n"] == 2


@pytest.mark.parametrize(
    ("code", "reason"),
    (
        ("rate_limit_exceeded", "capacity_before_claim"),
        ("durable_wake_in_progress", "live_owner_in_progress"),
        ("durable_wake_claim_unavailable", "claim_unavailable"),
        (
            "durable_wake_settlement_unavailable",
            "settlement_unavailable",
        ),
    ),
)
def test_exhausted_explicit_deferred_response_raises_typed_signal(
    monkeypatch,
    code,
    reason,
):
    """The durable carrier can release these retries without budget loss."""
    from aiohttp import web

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_RETRY_DELAYS_SECONDS", ())

    async def handler(request):
        return web.json_response(
            {"error": {"message": "retry", "code": code}},
            status=429,
            headers={"Retry-After": "2"},
        )

    async def run():
        runner, port = await _serve(handler)
        try:
            adapter = ApiServerLikeAdapter(port=port)
            with pytest.raises(
                wake_mod.DurableWakeDeferredError
            ) as raised:
                await deliver_wake(
                    adapter,
                    text="x",
                    session_id="sid",
                )
            assert raised.value.reason == reason
            assert raised.value.retry_after == 2.0
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_deliver_wake_raises_on_permanent_http_error(monkeypatch):
    """Auth/validation errors (403/400) are permanent — raise immediately so
    the caller can rewind instead of treating the event as delivered."""
    from aiohttp import web

    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return web.json_response({"error": "forbidden"}, status=403)

    async def run():
        runner, port = await _serve(handler)
        try:
            adapter = ApiServerLikeAdapter(port=port)
            with pytest.raises(RuntimeError, match="HTTP 403"):
                await deliver_wake(adapter, text="x", session_id="sid")
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert calls["n"] == 1


def test_deliver_wake_raises_after_exhausted_retries(monkeypatch):
    """Connection failures raise after bounded retries — never silent."""
    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_RETRY_DELAYS_SECONDS", (0.01,))
    # Nothing is listening on this port.
    adapter = ApiServerLikeAdapter(host="127.0.0.1", port=1, key="k")
    with pytest.raises(RuntimeError, match="gave up"):
        asyncio.run(deliver_wake(adapter, text="x", session_id="sid"))
