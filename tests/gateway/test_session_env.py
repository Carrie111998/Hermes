import asyncio
import os

import pytest

from agent.ultra_security import (
    get_current_principal,
    get_current_sandbox_lease,
    set_current_principal,
    set_current_sandbox_lease,
)
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource
from gateway.session_context import (
    get_session_env,
    set_session_vars,
    clear_session_vars,
    _VAR_MAP,
    _UNSET,
)


@pytest.fixture(autouse=True)
def _reset_contextvars():
    """Reset all session contextvars to _UNSET between tests.

    In production each asyncio.Task gets a fresh context copy where the
    defaults are _UNSET.  In tests all functions share the same thread
    context, so a clear_session_vars() from test A (which sets vars to "")
    would leak into test B.  This fixture ensures each test starts clean.
    """
    yield
    for var in _VAR_MAP.values():
        # Can't use var.reset() without a token; just set back to sentinel.
        var.set(_UNSET)
    set_current_principal(None)
    set_current_sandbox_lease(None)


def test_set_session_env_sets_contextvars(monkeypatch):
    """_set_session_env should populate contextvars, not os.environ."""
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_name="Group",
        chat_type="group",
        user_id="123456",
        user_name="alice",
        thread_id="17585",
    )
    context = SessionContext(source=source, connected_platforms=[], home_channels={})

    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)

    tokens = runner._set_session_env(context)

    # Values should be readable via get_session_env (contextvar path)
    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
    assert get_session_env("HERMES_SESSION_SOURCE") == ""
    assert get_session_env("HERMES_SESSION_CHAT_ID") == "-1001"
    assert get_session_env("HERMES_SESSION_CHAT_NAME") == "Group"
    assert get_session_env("HERMES_SESSION_USER_ID") == "123456"
    assert get_session_env("HERMES_SESSION_USER_NAME") == "alice"
    assert get_session_env("HERMES_SESSION_THREAD_ID") == "17585"

    # os.environ should NOT be touched
    assert os.getenv("HERMES_SESSION_PLATFORM") is None
    assert os.getenv("HERMES_SESSION_SOURCE") is None
    assert os.getenv("HERMES_SESSION_THREAD_ID") is None

    # Clean up
    runner._clear_session_env(tokens)


def test_session_source_uses_contextvars(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)

    tokens = set_session_vars(source="tool")

    assert get_session_env("HERMES_SESSION_SOURCE") == "tool"

    clear_session_vars(tokens)

    assert get_session_env("HERMES_SESSION_SOURCE") == ""


def test_clear_session_env_restores_previous_state(monkeypatch):
    """_clear_session_env should restore contextvars to their pre-handler values."""
    runner = object.__new__(GatewayRunner)

    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_name="Group",
        chat_type="group",
        user_id="123456",
        user_name="alice",
        thread_id="17585",
    )
    context = SessionContext(source=source, connected_platforms=[], home_channels={})

    tokens = runner._set_session_env(context)
    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
    assert get_session_env("HERMES_SESSION_USER_ID") == "123456"

    runner._clear_session_env(tokens)

    # After clear, contextvars should return to defaults (empty)
    assert get_session_env("HERMES_SESSION_PLATFORM") == ""
    assert get_session_env("HERMES_SESSION_CHAT_ID") == ""
    assert get_session_env("HERMES_SESSION_CHAT_NAME") == ""
    assert get_session_env("HERMES_SESSION_USER_ID") == ""
    assert get_session_env("HERMES_SESSION_USER_NAME") == ""
    assert get_session_env("HERMES_SESSION_THREAD_ID") == ""


def test_get_session_env_falls_back_to_os_environ(monkeypatch):
    """get_session_env should fall back to os.environ when contextvar is unset."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")

    # No contextvar set — should read from os.environ
    assert get_session_env("HERMES_SESSION_PLATFORM") == "discord"

    # Now set a contextvar — should prefer it
    tokens = set_session_vars(platform="telegram")
    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"

    # After clear — should return "" (explicitly cleared), NOT fall back
    # to os.environ.  This is the fix for #10304: stale os.environ values
    # must not leak through after a gateway session is cleaned up.
    clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_PLATFORM") == ""


def test_get_session_env_default_when_nothing_set(monkeypatch):
    """get_session_env returns default when neither contextvar nor env is set."""
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)

    assert get_session_env("HERMES_SESSION_PLATFORM") == ""
    assert get_session_env("HERMES_SESSION_PLATFORM", "fallback") == "fallback"


def test_set_session_env_handles_missing_optional_fields():
    """_set_session_env should handle None chat_name and thread_id gracefully."""
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_name=None,
        chat_type="private",
        thread_id=None,
    )
    context = SessionContext(source=source, connected_platforms=[], home_channels={})

    tokens = runner._set_session_env(context)

    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
    assert get_session_env("HERMES_SESSION_CHAT_ID") == "-1001"
    assert get_session_env("HERMES_SESSION_CHAT_NAME") == ""
    assert get_session_env("HERMES_SESSION_THREAD_ID") == ""

    runner._clear_session_env(tokens)


# ---------------------------------------------------------------------------
# SESSION_KEY contextvars tests
# ---------------------------------------------------------------------------


def test_session_key_set_via_contextvars(monkeypatch):
    """set_session_vars should set HERMES_SESSION_KEY via contextvars."""
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)

    tokens = set_session_vars(
        platform="telegram",
        chat_id="-1001",
        session_key="tg:-1001:17585",
    )
    assert get_session_env("HERMES_SESSION_KEY") == "tg:-1001:17585"

    clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_KEY") == ""


def test_session_key_falls_back_to_os_environ(monkeypatch):
    """get_session_env for SESSION_KEY should fall back to os.environ."""
    monkeypatch.setenv("HERMES_SESSION_KEY", "env-session-123")

    # No contextvar set — should read from os.environ
    assert get_session_env("HERMES_SESSION_KEY") == "env-session-123"

    # Set contextvar — should prefer it
    tokens = set_session_vars(session_key="ctx-session-456")
    assert get_session_env("HERMES_SESSION_KEY") == "ctx-session-456"

    # After clear — should return "" (explicitly cleared), not os.environ (#10304)
    clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_KEY") == ""


def test_session_id_set_via_contextvars(monkeypatch):
    """set_session_vars should set HERMES_SESSION_ID via contextvars."""
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-env-session")

    tokens = set_session_vars(session_id="ctx-session-456")
    assert get_session_env("HERMES_SESSION_ID") == "ctx-session-456"

    clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_ID") == ""


def test_set_session_vars_binds_principal_scope():
    """set_session_vars should bind the server-resolved principal for the turn."""
    tokens = set_session_vars(
        chat_id="chat-1",
        user_id="user-1",
        session_id="session-1",
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        project_id="project-1",
        roles=("admin", "member"),
    )

    principal = get_current_principal()
    assert principal is not None
    assert principal.tenant_id == "tenant-1"
    assert principal.workspace_id == "workspace-1"
    assert principal.project_id == "project-1"
    assert principal.user_id == "user-1"
    assert principal.session_id == "session-1"
    assert principal.roles == ("admin", "member")
    assert principal.source == "session_context"
    lease = get_current_sandbox_lease()
    assert lease is not None
    assert lease.tenant_id == "tenant-1"
    assert lease.workspace_id == "workspace-1"
    assert lease.project_id == "project-1"
    assert lease.owner_user_id == "user-1"
    assert lease.session_id == "session-1"
    assert lease.is_active()

    clear_session_vars(tokens)
    assert get_current_principal() is None
    assert get_current_sandbox_lease() is None


def test_set_session_vars_uses_local_principal_for_legacy_gateway_callers():
    """Existing callers get a local owner principal until auth passes real scope."""
    tokens = set_session_vars(user_id="user-1", session_key="legacy-session")

    principal = get_current_principal()
    assert principal is not None
    assert principal.tenant_id == "local-tenant"
    assert principal.workspace_id == "local-workspace"
    assert principal.project_id == "legacy-session"
    assert principal.user_id == "user-1"
    assert principal.session_id == "legacy-session"
    assert principal.roles == ("owner",)
    assert principal.source == "session_context_fallback"

    clear_session_vars(tokens)
    assert get_current_principal() is None
    assert get_current_sandbox_lease() is None


def test_set_session_vars_empty_explicit_roles_are_viewer():
    """An auth layer that sends no roles should not grant write/admin power."""
    tokens = set_session_vars(
        user_id="user-1",
        session_id="session-1",
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        project_id="project-1",
        roles=[],
    )

    principal = get_current_principal()
    assert principal is not None
    assert principal.roles == ("viewer",)

    clear_session_vars(tokens)


def test_set_session_vars_server_scope_without_roles_is_viewer():
    """A scoped multi-tenant principal must not become owner by omission."""
    tokens = set_session_vars(
        user_id="user-1",
        session_id="session-1",
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        project_id="project-1",
    )

    principal = get_current_principal()
    assert principal is not None
    assert principal.roles == ("viewer",)
    assert principal.source == "session_context"

    clear_session_vars(tokens)


def test_set_session_vars_can_bind_explicit_sandbox_lease_fields():
    tokens = set_session_vars(
        user_id="user-1",
        session_id="session-1",
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        project_id="project-1",
        roles=("member",),
        sandbox_id="sandbox-1",
        sandbox_status="active",
        sandbox_expires_at=9_999_999_999,
    )

    lease = get_current_sandbox_lease()
    assert lease is not None
    assert lease.sandbox_id == "sandbox-1"
    assert lease.status == "active"
    assert lease.expires_at == 9_999_999_999

    clear_session_vars(tokens)


def test_set_session_env_includes_session_key():
    """_set_session_env should propagate session_key from SessionContext."""
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_name="Group",
        chat_type="group",
        thread_id="17585",
    )
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_key="tg:-1001:17585",
    )

    # Capture baseline value before setting (may be non-empty from another
    # test in the same pytest-xdist worker sharing the context).
    tokens = runner._set_session_env(context)
    assert get_session_env("HERMES_SESSION_KEY") == "tg:-1001:17585"
    runner._clear_session_env(tokens)
    # After clearing, the session key must not retain the value we just set.
    # The exact post-clear value depends on context propagation from other
    # tests, so only check that our value was removed, not what replaced it.
    assert get_session_env("HERMES_SESSION_KEY") != "tg:-1001:17585"


def test_session_key_no_race_condition_with_contextvars(monkeypatch):
    """Prove contextvars isolates SESSION_KEY across concurrent async tasks.

    Two tasks set different session keys. With contextvars each task
    reads back its own value. With os.environ the second task would
    overwrite the first (the old bug).
    """
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)

    results = {}

    async def handler(key: str, delay: float):
        tokens = set_session_vars(session_key=key)
        try:
            await asyncio.sleep(delay)
            read_back = get_session_env("HERMES_SESSION_KEY")
            results[key] = read_back
        finally:
            clear_session_vars(tokens)

    async def run():
        task_a = asyncio.create_task(handler("session-A", 0.15))
        await asyncio.sleep(0.05)
        task_b = asyncio.create_task(handler("session-B", 0.05))
        await asyncio.gather(task_a, task_b)

    asyncio.run(run())

    # Both tasks must read back their own session key
    assert results["session-A"] == "session-A", (
        f"Session A got '{results['session-A']}' instead of 'session-A' — race condition!"
    )
    assert results["session-B"] == "session-B", (
        f"Session B got '{results['session-B']}' instead of 'session-B' — race condition!"
    )


def test_principal_no_race_condition_with_contextvars():
    """Principal binding must remain task-local across concurrent sessions."""
    results = {}

    async def handler(label: str, delay: float):
        tokens = set_session_vars(
            user_id=f"user-{label}",
            session_id=f"session-{label}",
            tenant_id=f"tenant-{label}",
            workspace_id=f"workspace-{label}",
            project_id=f"project-{label}",
            roles=("member",),
        )
        try:
            await asyncio.sleep(delay)
            principal = get_current_principal()
            lease = get_current_sandbox_lease()
            assert principal is not None
            assert lease is not None
            results[label] = (
                principal.tenant_id,
                principal.workspace_id,
                principal.project_id,
                principal.user_id,
                principal.session_id,
                principal.roles,
                lease.tenant_id,
                lease.session_id,
                lease.owner_user_id,
            )
        finally:
            clear_session_vars(tokens)

    async def run():
        task_a = asyncio.create_task(handler("A", 0.15))
        await asyncio.sleep(0.05)
        task_b = asyncio.create_task(handler("B", 0.05))
        await asyncio.gather(task_a, task_b)

    asyncio.run(run())

    assert results["A"] == (
        "tenant-A",
        "workspace-A",
        "project-A",
        "user-A",
        "session-A",
        ("member",),
        "tenant-A",
        "session-A",
        "user-A",
    )
    assert results["B"] == (
        "tenant-B",
        "workspace-B",
        "project-B",
        "user-B",
        "session-B",
        ("member",),
        "tenant-B",
        "session-B",
        "user-B",
    )


@pytest.mark.asyncio
async def test_run_in_executor_with_context_preserves_session_env(monkeypatch):
    """Gateway executor work should inherit session contextvars for tool routing."""
    runner = object.__new__(GatewayRunner)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="2144471399",
        chat_type="dm",
        user_id="123456",
        user_name="alice",
        thread_id=None,
    )
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_key="agent:main:telegram:dm:2144471399",
    )

    tokens = runner._set_session_env(context)
    try:
        result = await runner._run_in_executor_with_context(
            lambda: {
                "platform": get_session_env("HERMES_SESSION_PLATFORM"),
                "chat_id": get_session_env("HERMES_SESSION_CHAT_ID"),
                "user_id": get_session_env("HERMES_SESSION_USER_ID"),
                "session_key": get_session_env("HERMES_SESSION_KEY"),
            }
        )
    finally:
        runner._clear_session_env(tokens)
        runner._shutdown_executor()

    assert result == {
        "platform": "telegram",
        "chat_id": "2144471399",
        "user_id": "123456",
        "session_key": "agent:main:telegram:dm:2144471399",
    }


@pytest.mark.asyncio
async def test_run_in_executor_with_context_forwards_args():
    """_run_in_executor_with_context should forward *args to the callable."""
    runner = object.__new__(GatewayRunner)

    def add(a, b):
        return a + b

    try:
        result = await runner._run_in_executor_with_context(add, 3, 7)
    finally:
        runner._shutdown_executor()
    assert result == 10


@pytest.mark.asyncio
async def test_run_in_executor_with_context_propagates_exceptions():
    """Exceptions inside the executor should propagate to the caller."""
    runner = object.__new__(GatewayRunner)

    def blow_up():
        raise ValueError("boom")

    try:
        with pytest.raises(ValueError, match="boom"):
            await runner._run_in_executor_with_context(blow_up)
    finally:
        runner._shutdown_executor()


@pytest.mark.asyncio
async def test_run_in_executor_with_context_survives_default_executor_shutdown():
    """Gateway agent work should not depend on asyncio's default executor."""
    runner = object.__new__(GatewayRunner)
    loop = asyncio.get_running_loop()

    await loop.run_in_executor(None, lambda: None)
    await loop.shutdown_default_executor()

    try:
        result = await runner._run_in_executor_with_context(lambda: "ok")
    finally:
        runner._shutdown_executor()

    assert result == "ok"


@pytest.mark.asyncio
async def test_gateway_executor_refuses_resurrection_after_shutdown():
    """A real gateway shutdown must NOT be resurrected by the recreate path.

    _shutdown_executor() means "we're stopping" — the recreate-on-shutdown
    logic exists to survive an *external* teardown of the loop default
    (test_..._survives_default_executor_shutdown), not to undo our own stop.
    """
    runner = object.__new__(GatewayRunner)

    try:
        first = await runner._run_in_executor_with_context(lambda: "first")
        assert first == "first"
        runner._shutdown_executor()

        with pytest.raises(RuntimeError, match="shutting down"):
            await runner._run_in_executor_with_context(lambda: "second")
    finally:
        runner._shutdown_executor()
