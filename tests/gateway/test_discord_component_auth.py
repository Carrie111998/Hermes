"""Security regression tests: Discord component views honor allowlists.

The interactive component views (ExecApprovalView, SlashConfirmView,
UpdatePromptView, ModelPickerView, ClarifyChoiceView) historically accepted only
``allowed_user_ids``. Deployments that configure DISCORD_ALLOWED_ROLES
without DISCORD_ALLOWED_USERS therefore had a wide-open component
surface: any guild member who could see the prompt could approve exec
commands, cancel slash confirmations, or switch the model -- even when
the same user would be rejected at the slash and on_message gates.

These tests pin user/role/global allowlist semantics, explicit allow-all
handling, and fail-closed behavior so the parity cannot regress.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig

# Trigger the shared discord mock from tests/gateway/conftest.py before
# importing the production module.
from plugins.platforms.discord.adapter import (  # noqa: E402
    ClarifyChoiceView,
    DiscordAdapter,
    ExecApprovalView,
    ModelPickerView,
    SlashConfirmView,
    UpdatePromptView,
    _component_check_auth,
    _resolve_exec_approval_admin_gate,
)


@pytest.fixture(autouse=True)
def _clear_component_auth_env(monkeypatch):
    from unittest.mock import MagicMock, patch

    for name in (
        "DISCORD_ALLOW_ALL_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
    ):
        monkeypatch.delenv(name, raising=False)

    # Default-mock PairingStore so tests don't hit the filesystem.
    # Pairing-specific tests override this with explicit mock values.
    mock_store = MagicMock()
    mock_store.is_approved.return_value = False
    with patch("gateway.pairing.PairingStore", return_value=mock_store):
        yield


# ---------------------------------------------------------------------------
# Direct helper coverage -- the views all delegate to this helper, so
# pinning the helper's contract pins all call sites.
# ---------------------------------------------------------------------------


def _interaction(user_id, role_ids=None, *, drop_user=False, drop_roles=False):
    """Build a mock interaction with the requested user/role shape.

    drop_user simulates a payload whose .user attribute is None.
    drop_roles simulates a payload where .user has no .roles attribute
    at all (DM-context Member, raw User payload).
    """
    if drop_user:
        return SimpleNamespace(user=None)

    user_kwargs = {"id": user_id}
    if not drop_roles:
        user_kwargs["roles"] = [SimpleNamespace(id=r) for r in (role_ids or [])]
    return SimpleNamespace(user=SimpleNamespace(**user_kwargs))


# ── no policy configured -> deny unless allow-all is explicit ──────────────


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("DISCORD_ALLOW_ALL_USERS", "true"),
        ("GATEWAY_ALLOW_ALL_USERS", "yes"),
    ],
)
def test_component_check_explicit_allow_all_passes(monkeypatch, env_name, env_value):
    monkeypatch.setenv(env_name, env_value)
    interaction = _interaction(11111)
    assert _component_check_auth(interaction, set(), set()) is True


# ── user allowlist ─────────────────────────────────────────────────────────


# ── role allowlist OR semantics ────────────────────────────────────────────


# ── fail-closed on missing role data ───────────────────────────────────────


# ---------------------------------------------------------------------------
# View construction: every view must accept allowed_role_ids and route
# through the shared helper. Default value preserves prior call-sites.
# ---------------------------------------------------------------------------


def test_exec_approval_view_accepts_role_allowlist():
    view = ExecApprovalView(
        session_key="sess-1",
        allowed_user_ids={"11111"},
        allowed_role_ids={42},
    )
    # Role-only user passes
    assert view._check_auth(_interaction(99999, role_ids=[42])) is True
    # Neither user nor role match: reject
    assert view._check_auth(_interaction(99999, role_ids=[7])) is False


def test_slash_confirm_view_accepts_role_allowlist():
    view = SlashConfirmView(
        session_key="sess-1",
        confirm_id="c1",
        allowed_user_ids=set(),
        allowed_role_ids={42},
    )
    assert view._check_auth(_interaction(99999, role_ids=[42])) is True
    assert view._check_auth(_interaction(99999, role_ids=[7])) is False


def test_update_prompt_view_accepts_role_allowlist():
    view = UpdatePromptView(
        session_key="sess-1",
        allowed_user_ids=set(),
        allowed_role_ids={42},
    )
    assert view._check_auth(_interaction(99999, role_ids=[42])) is True
    assert view._check_auth(_interaction(99999, role_ids=[7])) is False


@pytest.mark.asyncio
async def test_update_prompt_response_is_correlation_bound_and_replay_inert(tmp_path):
    pending = {
        "correlation_id": "corr-1",
        "user_id": "1",
        "session_key": "session-1",
        "origin_profile": "work",
        "profile_home": "/profiles/work",
        "control_home": str(tmp_path),
        "install_root": "/project/hermes",
        "install_id": "install-1",
    }
    prompt = {
        "id": "prompt-1",
        "kind": "update_confirmation",
        "correlation_id": "corr-1",
        "context": {
            "origin_profile": "work",
            "profile_home": "/profiles/work",
            "control_home": str(tmp_path),
            "install_root": "/project/hermes",
            "install_id": "install-1",
        },
    }
    (tmp_path / ".update_pending.json").write_text(json.dumps(pending))
    (tmp_path / ".update_prompt.json").write_text(json.dumps(prompt))
    view = UpdatePromptView(
        session_key="session-1",
        prompt_id="prompt-1",
        correlation_id="corr-1",
        control_home=str(tmp_path),
        allowed_user_ids={"1"},
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, display_name="Operator", roles=[]),
        message=SimpleNamespace(embeds=[]),
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
    )

    await view._respond(interaction, "y", MagicMock(), "Yes")

    assert json.loads((tmp_path / ".update_response").read_text()) == {
        "answer": "yes",
        "correlation_id": "corr-1",
        "id": "prompt-1",
    }
    interaction.response.edit_message.assert_awaited_once()

    await view._respond(interaction, "n", MagicMock(), "No")
    assert "already" in interaction.response.send_message.call_args.args[0].lower()


def test_clarify_choice_view_accepts_role_allowlist():
    view = ClarifyChoiceView(
        choices=["one", "two"],
        clarify_id="clarify-1",
        allowed_user_ids=set(),
        allowed_role_ids={42},
    )
    assert view._check_auth(_interaction(99999, role_ids=[42])) is True
    assert view._check_auth(_interaction(99999, role_ids=[7])) is False


# ---------------------------------------------------------------------------
# Empty allowlists across views: fail closed unless allow-all is explicit.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "view_factory",
    [
        lambda: ExecApprovalView(session_key="s", allowed_user_ids=set()),
        lambda: SlashConfirmView(session_key="s", confirm_id="c", allowed_user_ids=set()),
        lambda: UpdatePromptView(session_key="s", allowed_user_ids=set()),
        lambda: ClarifyChoiceView(
            choices=["one"],
            clarify_id="c",
            allowed_user_ids=set(),
        ),
    ],
)
def test_views_empty_allowlists_reject_by_default(view_factory, monkeypatch):
    monkeypatch.delenv("DISCORD_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    view = view_factory()
    assert view._check_auth(_interaction(99999)) is False


def test_model_picker_view_empty_allowlists_reject_by_default(monkeypatch):
    monkeypatch.delenv("DISCORD_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)

    async def _noop(*_a, **_k):
        return ""

    view = ModelPickerView(
        providers=[],
        current_model="m",
        current_provider="p",
        session_key="s",
        on_model_selected=_noop,
        allowed_user_ids=set(),
    )
    assert view.allowed_role_ids == set()
    assert view._check_auth(_interaction(99999)) is False


def test_view_empty_allowlists_allow_with_explicit_allow_all(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    view = ExecApprovalView(session_key="s", allowed_user_ids=set())
    assert view._check_auth(_interaction(99999)) is True


def _approval_interaction(user_id=1):
    class _Embed:
        footer = {}

        def set_footer(self, *, text):
            self.footer = {"text": text}

    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name="Operator", roles=[]),
        message=SimpleNamespace(embeds=[_Embed()]),
        response=SimpleNamespace(
            edit_message=AsyncMock(),
            send_message=AsyncMock(),
        ),
    )


def _capture_component_route(adapter):
    sent = {}

    async def _send(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(id=9001)

    channel = SimpleNamespace(send=AsyncMock(side_effect=_send))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )
    return sent


@pytest.mark.asyncio
async def test_exec_approval_component_route_rejects_unauthorized_click():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._allowed_user_ids = {"1"}
    sent = _capture_component_route(adapter)

    result = await adapter.send_exec_approval("555", "echo hi", "session-1")
    assert result.success is True
    view = sent["view"]
    interaction = _approval_interaction(user_id=2)

    with patch("tools.approval.resolve_gateway_approval") as resolve:
        await view.allow_once(interaction, MagicMock())

    resolve.assert_not_called()
    assert view.resolved is False
    interaction.response.send_message.assert_awaited_once()
    assert "not authorized" in interaction.response.send_message.call_args.args[0]


@pytest.mark.asyncio
async def test_exec_approval_component_route_marks_mismatched_stale_click():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._allowed_user_ids = {"1"}
    sent = _capture_component_route(adapter)

    result = await adapter.send_exec_approval("555", "echo hi", "session-1")
    assert result.success is True
    interaction = _approval_interaction()

    with patch("tools.approval.resolve_gateway_approval", return_value=0) as resolve:
        await sent["view"].allow_once(interaction, MagicMock())

    resolve.assert_called_once_with("session-1", "once")
    assert sent["view"].resolved is True
    interaction.response.edit_message.assert_awaited_once()
    assert "expired" in interaction.message.embeds[0].footer["text"]


@pytest.mark.asyncio
async def test_update_prompt_component_route_rejects_writer_failure(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._allowed_user_ids = {"1"}
    sent = _capture_component_route(adapter)

    result = await adapter.send_update_prompt(
        "555",
        "Continue update?",
        session_key="session-1",
        prompt_id="prompt-1",
        correlation_id="corr-1",
        context={"control_home": str(tmp_path)},
    )
    assert result.success is True

    interaction = _approval_interaction()
    with patch(
        "gateway.update_prompt_response.write_update_confirmation_response",
        return_value=False,
    ) as writer:
        await sent["view"].yes_btn(interaction, MagicMock())

    writer.assert_called_once()
    assert sent["view"].resolved is False
    interaction.response.edit_message.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    assert "stale" in interaction.response.send_message.call_args.args[0]


@pytest.mark.asyncio
async def test_update_prompt_component_route_allows_only_one_concurrent_writer(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._allowed_user_ids = {"1", "2"}
    sent = _capture_component_route(adapter)

    result = await adapter.send_update_prompt(
        "555",
        "Continue update?",
        session_key="session-1",
        prompt_id="prompt-1",
        correlation_id="corr-1",
        context={"control_home": str(tmp_path)},
    )
    assert result.success is True
    view = sent["view"]
    first = _approval_interaction(user_id=1)
    second = _approval_interaction(user_id=2)

    with patch(
        "gateway.update_prompt_response.write_update_confirmation_response",
        return_value=True,
    ) as writer:
        await asyncio.gather(
            view.yes_btn(first, MagicMock()),
            view.no_btn(second, MagicMock()),
        )

    writer.assert_called_once()
    assert view.resolved is True
    assert first.response.edit_message.await_count + second.response.edit_message.await_count == 1
    assert first.response.send_message.await_count + second.response.send_message.await_count == 1


@pytest.mark.asyncio
async def test_exec_approval_public_button_rejects_unauthorized_click():
    view = ExecApprovalView(session_key="s", allowed_user_ids={"1"})
    interaction = _approval_interaction(user_id=2)

    with patch("tools.approval.resolve_gateway_approval") as resolve:
        await view.allow_once(interaction, MagicMock())

    resolve.assert_not_called()
    assert view.resolved is False
    interaction.response.send_message.assert_awaited_once()
    assert "not authorized" in interaction.response.send_message.call_args.args[0]


@pytest.mark.asyncio
async def test_exec_approval_public_button_marks_stale_resolution_without_approval():
    view = ExecApprovalView(session_key="s", allowed_user_ids={"1"})
    interaction = _approval_interaction()

    with patch("tools.approval.resolve_gateway_approval", return_value=0) as resolve:
        await view.allow_once(interaction, MagicMock())

    resolve.assert_called_once_with("s", "once")
    assert view.resolved is True
    interaction.response.edit_message.assert_awaited_once()
    assert "expired" in interaction.message.embeds[0].footer["text"]


@pytest.mark.asyncio
async def test_exec_approval_resolution_logs_redacted_transport_error(caplog):
    view = ExecApprovalView(session_key="s", allowed_user_ids={"1"})
    interaction = _approval_interaction()
    secret = "synthetic-discord-transport-secret-1234567890"

    with patch(
        "tools.approval.resolve_gateway_approval",
        side_effect=RuntimeError(f"transport Authorization: Bearer {secret}"),
    ):
        with caplog.at_level("ERROR", logger="plugins.platforms.discord.adapter"):
            await view.allow_once(interaction, MagicMock())

    assert secret not in caplog.text
    assert "Failed to resolve gateway approval from button" in caplog.text
    interaction.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_prompt_public_button_rejects_writer_failure(tmp_path):
    view = UpdatePromptView(
        session_key="s",
        prompt_id="p",
        correlation_id="corr",
        control_home=str(tmp_path),
        allowed_user_ids={"1"},
    )
    interaction = _approval_interaction()

    with patch(
        "gateway.update_prompt_response.write_update_confirmation_response",
        return_value=False,
    ) as writer:
        await view.yes_btn(interaction, MagicMock())

    writer.assert_called_once()
    assert view.resolved is False
    interaction.response.edit_message.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    assert "stale" in interaction.response.send_message.call_args.args[0]


@pytest.mark.asyncio
async def test_update_prompt_public_button_replay_is_inert(tmp_path):
    view = UpdatePromptView(
        session_key="s",
        prompt_id="p",
        correlation_id="corr",
        control_home=str(tmp_path),
        allowed_user_ids={"1"},
    )
    interaction = _approval_interaction()

    with patch(
        "gateway.update_prompt_response.write_update_confirmation_response",
        return_value=True,
    ) as writer:
        await view.yes_btn(interaction, MagicMock())
        await view.no_btn(interaction, MagicMock())

    writer.assert_called_once()
    assert view.resolved is True
    assert interaction.response.edit_message.await_count == 1
    assert interaction.response.send_message.await_count == 1
    assert "already" in interaction.response.send_message.call_args.args[0].lower()


# ---------------------------------------------------------------------------
# Pairing store: users approved via ``hermes pairing approve`` must be
# authorized even without DISCORD_ALLOWED_USERS / DISCORD_ALLOWED_ROLES.
# ---------------------------------------------------------------------------


def test_component_check_pairing_approved_user_passes(monkeypatch):
    """User approved in pairing store passes even without allowlists."""
    from unittest.mock import MagicMock, patch

    mock_store = MagicMock()
    mock_store.is_approved.return_value = True
    # Override the autouse fixture's mock with approved=True
    with patch("gateway.pairing.PairingStore", return_value=mock_store):
        interaction = _interaction(11111)
        assert _component_check_auth(interaction, set(), set()) is True
    mock_store.is_approved.assert_called_once_with("discord", "11111")


# ---------------------------------------------------------------------------
# Opt-in admin gate for exec-approval buttons (feat/discord-admin-exec-approval).
# Default OFF: any admitted user can approve (the v0.16-restored behavior).
# When `require_admin_for_exec_approval` is true, the clicker must ALSO be in
# `allow_admin_from`. Fails closed (logged) when the toggle is on but no
# admins are configured. Only ExecApprovalView is gated — other views stay
# user-scope.
# ---------------------------------------------------------------------------


def test_admin_gate_resolver_on_parses_admins():
    """Toggle true -> gate enabled, admins coerced from allow_admin_from."""
    require_admin, admins = _resolve_exec_approval_admin_gate(
        {"require_admin_for_exec_approval": True, "allow_admin_from": "111, 222"}
    )
    assert require_admin is True
    assert admins == {"111", "222"}
    # list form normalizes identically
    _, admins_list = _resolve_exec_approval_admin_gate(
        {"require_admin_for_exec_approval": "true", "allow_admin_from": [111, 222]}
    )
    assert admins_list == {"111", "222"}


def test_exec_view_gate_on_non_admin_rejected():
    """Gate on: admitted user who is NOT an admin is rejected at the button."""
    view = ExecApprovalView(
        session_key="s",
        allowed_user_ids={"11111", "22222"},
        require_admin=True,
        admin_user_ids={"11111"},
    )
    # 22222 is admitted (in allowlist) but not an admin -> rejected.
    assert view._check_auth(_interaction(22222)) is False


def test_exec_view_gate_on_no_admins_fails_closed(caplog):
    """Gate on but no admins configured -> nobody approves, logged once."""
    import logging

    view = ExecApprovalView(
        session_key="s",
        allowed_user_ids={"11111"},
        require_admin=True,
        admin_user_ids=set(),
    )
    with caplog.at_level(logging.WARNING):
        assert view._check_auth(_interaction(11111)) is False
    assert any(
        "require_admin_for_exec_approval" in r.message for r in caplog.records
    )


def test_other_views_not_admin_gated():
    """Lower-stakes views never take the admin gate — they stay user-scope."""
    # SlashConfirmView/ModelPickerView/etc. construct without require_admin and
    # delegate straight to _component_check_auth.
    sc = SlashConfirmView(
        session_key="s", confirm_id="c", allowed_user_ids={"11111"}
    )
    assert sc._check_auth(_interaction(11111)) is True
