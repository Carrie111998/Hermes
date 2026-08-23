"""Feishu interactive-card click authorization gates.

Behavior contract: an interactive card (exec approval / update prompt) is
answerable under the admission rules of the chat it was sent to. A DM card
is visible only to the DM peer, so the peer who triggered the turn must be
able to answer it — including under the empty-``FEISHU_ALLOWED_USERS``
pairing default, where DM *messages* are admitted. Group cards keep the
group policy gate (``_allow_group_message``).

Regression (live incident, 2026-08-23 gateway.log): the click gate ran
EVERY card through the group policy. ``FEISHU_GROUP_POLICY`` defaults to
``allowlist`` and the allowlist was empty, so every DM approval click was
discarded with "Unauthorized approval click" — the approval timed out as
deny, the agent re-requested, and the user could never confirm a git
clone. Meanwhile a second gate (``_is_interactive_operator_authorized``)
had the OPPOSITE empty-set semantics (empty = allow) and was unreachable
behind the first. The two gates are now one chat-aware predicate.
"""

import asyncio
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    import lark_oapi  # noqa: F401
    _HAS_LARK_OAPI = True
except ImportError:
    _HAS_LARK_OAPI = False


def _make_adapter(**env):
    """Construct a FeishuAdapter under a controlled environment.

    Defaults reproduce the incident configuration: stock group policy
    (``allowlist``) and an empty user allowlist (pairing-mode default).
    """
    from gateway.config import PlatformConfig
    from plugins.platforms.feishu.adapter import FeishuAdapter

    base = {
        "FEISHU_APP_ID": "cli_test",
        "FEISHU_APP_SECRET": "secret_test",
        "FEISHU_ALLOWED_USERS": "",
        "FEISHU_ALLOW_ALL_USERS": "",
        "GATEWAY_ALLOW_ALL_USERS": "",
        "FEISHU_GROUP_POLICY": "allowlist",
    }
    base.update(env)
    with patch.dict(os.environ, base, clear=False):
        return FeishuAdapter(PlatformConfig())


def _dm_approval_state() -> dict:
    return {
        "session_key": "agent:main:feishu:dm:oc_dm",
        "message_id": "om_1",
        "chat_id": "oc_dm",
        "chat_type": "dm",
    }


def _group_approval_state() -> dict:
    return {
        "session_key": "agent:main:feishu:group:oc_grp",
        "message_id": "om_2",
        "chat_id": "oc_grp",
        "chat_type": "group",
    }


def _card_event(open_id: str, chat_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        operator=SimpleNamespace(open_id=open_id, user_id=""),
        context=SimpleNamespace(open_chat_id=chat_id),
    )


class TestCardOperatorGate(unittest.TestCase):
    """_is_card_operator_authorized mirrors the card chat's admission rules."""

    def test_dm_card_click_authorized_under_pairing_default(self):
        """Empty allowlist = pairing-mode default: the DM peer may answer.

        This is the exact incident configuration — DM messages flow, so the
        DM peer's approval clicks must flow too.
        """
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="")
        state = _dm_approval_state()
        self.assertTrue(adapter._is_card_operator_authorized(state, "ou_peer"))

    def test_dm_card_click_denied_when_allowlist_excludes_clicker(self):
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="ou_alice")
        state = _dm_approval_state()
        self.assertFalse(adapter._is_card_operator_authorized(state, "ou_bob"))

    def test_dm_card_click_authorized_when_clicker_in_allowlist(self):
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="ou_alice")
        state = _dm_approval_state()
        self.assertTrue(adapter._is_card_operator_authorized(state, "ou_alice"))

    def test_dm_card_click_authorized_by_gateway_allow_all(self):
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="ou_alice")
        state = _dm_approval_state()
        with patch.dict(os.environ, {"GATEWAY_ALLOW_ALL_USERS": "true"}, clear=False):
            self.assertTrue(adapter._is_card_operator_authorized(state, "ou_anyone"))

    def test_dm_card_click_authorized_for_admin(self):
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="ou_alice")
        adapter._admins = {"ou_admin"}
        state = _dm_approval_state()
        self.assertTrue(adapter._is_card_operator_authorized(state, "ou_admin"))

    def test_group_card_click_still_gated_by_group_policy(self):
        """Group cards keep the fail-closed group gate: empty allowlist
        under the default ``allowlist`` policy denies non-admin clickers."""
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="")
        state = _group_approval_state()
        self.assertFalse(adapter._is_card_operator_authorized(state, "ou_member"))

    def test_group_card_click_authorized_for_allowlisted_member(self):
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="ou_alice")
        state = _group_approval_state()
        self.assertTrue(adapter._is_card_operator_authorized(state, "ou_alice"))

    def test_state_without_chat_type_falls_back_to_chat_info_cache(self):
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="")
        adapter._chat_info_cache["oc_dm"] = {"chat_id": "oc_dm", "name": "dm", "type": "dm"}
        state = {"chat_id": "oc_dm"}  # legacy state: no chat_type recorded
        self.assertTrue(adapter._is_card_operator_authorized(state, "ou_peer"))

    def test_unknown_chat_type_fails_closed_to_group_gate(self):
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="ou_alice")
        state = {"chat_id": "oc_unresolved"}  # no state type, no cache entry
        self.assertFalse(adapter._is_card_operator_authorized(state, "ou_bob"))


class TestSendRecordsChatType(unittest.TestCase):
    """Card sends record the chat type for the click gate."""

    @staticmethod
    def _adapter_with_send_mocked(chat_type: str, chat_id: str):
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="")
        adapter._client = object()
        adapter._feishu_send_with_retry = AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="om_x")
        )
        adapter._finalize_send_result = lambda response, ctx: SimpleNamespace(
            success=True, message_id="om_x"
        )
        adapter.get_chat_info = AsyncMock(
            return_value={"chat_id": chat_id, "type": chat_type, "name": chat_id}
        )
        return adapter

    def test_send_exec_approval_records_dm_chat_type(self):
        adapter = self._adapter_with_send_mocked("dm", "oc_dm")
        result = asyncio.run(
            adapter.send_exec_approval(
                "oc_dm", "git clone https://github.com/x/y", "agent:main:feishu:dm:oc_dm"
            )
        )
        self.assertTrue(result.success)
        state = adapter._approval_state[1]
        self.assertEqual(state["chat_type"], "dm")
        self.assertEqual(state["chat_id"], "oc_dm")

    def test_send_update_prompt_records_group_chat_type(self):
        adapter = self._adapter_with_send_mocked("group", "oc_grp")
        result = asyncio.run(
            adapter.send_update_prompt(
                "oc_grp", "Proceed?", session_key="agent:main:feishu:group:oc_grp"
            )
        )
        self.assertTrue(result.success)
        state = adapter._update_prompt_state[1]
        self.assertEqual(state["chat_type"], "group")


class TestApprovalResolutionPath(unittest.TestCase):
    """The click actually unblocks the waiting agent for DM-origin approvals."""

    @staticmethod
    def _cache_name(adapter, open_id: str, name: str) -> None:
        adapter._sender_name_cache[open_id] = (name, time.time() + 300)

    def test_dm_peer_click_resolves_approval(self):
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="")
        self._cache_name(adapter, "ou_peer", "Peer")
        adapter._approval_state[1] = _dm_approval_state()
        with patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve:
            asyncio.run(
                adapter._resolve_approval(1, "once", "Peer", open_id="ou_peer", chat_id="oc_dm")
            )
        resolve.assert_called_once_with("agent:main:feishu:dm:oc_dm", "once")
        self.assertNotIn(1, adapter._approval_state, "approved card state must be popped")

    def test_group_click_without_allowlist_does_not_resolve(self):
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="")
        adapter._approval_state[1] = _group_approval_state()
        with patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve:
            asyncio.run(
                adapter._resolve_approval(1, "once", "Member", open_id="ou_member", chat_id="oc_grp")
            )
        resolve.assert_not_called()
        self.assertIn(1, adapter._approval_state, "denied card state must stay pending")

    def test_dm_update_prompt_click_persists_answer(self):
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="")
        adapter._update_prompt_state[1] = {
            "session_key": "agent:main:feishu:dm:oc_dm",
            "message_id": "om_3",
            "chat_id": "oc_dm",
            "chat_type": "dm",
        }
        with patch.object(adapter, "_write_update_prompt_response") as write:
            asyncio.run(
                adapter._resolve_update_prompt(1, "y", "Peer", open_id="ou_peer", chat_id="oc_dm")
            )
        write.assert_called_once_with("y")
        self.assertNotIn(1, adapter._update_prompt_state)

    def test_group_update_prompt_click_without_allowlist_is_dropped(self):
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="")
        adapter._update_prompt_state[1] = {
            "session_key": "agent:main:feishu:group:oc_grp",
            "message_id": "om_4",
            "chat_id": "oc_grp",
            "chat_type": "group",
        }
        with patch.object(adapter, "_write_update_prompt_response") as write:
            asyncio.run(
                adapter._resolve_update_prompt(1, "y", "Member", open_id="ou_member", chat_id="oc_grp")
            )
        write.assert_not_called()
        self.assertIn(1, adapter._update_prompt_state)


@unittest.skipUnless(_HAS_LARK_OAPI, "lark-oapi not installed")
class TestSyncCardActionHandler(unittest.TestCase):
    """The synchronous card-action callback answers the DM peer inline."""

    @classmethod
    def setUpClass(cls):
        from plugins.platforms.feishu import adapter as feishu_adapter

        assert feishu_adapter._load_lark_oapi(), "lark_oapi import failed"

    @staticmethod
    def _drain(loop: asyncio.AbstractEventLoop) -> None:
        async def _run_pending() -> None:
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        loop.run_until_complete(_run_pending())

    def test_dm_peer_click_resolves_inline(self):
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="")
        adapter._sender_name_cache["ou_peer"] = ("Peer", time.time() + 300)
        loop = asyncio.new_event_loop()
        adapter._loop = loop
        adapter._approval_state[1] = _dm_approval_state()
        try:
            with patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve:
                response = adapter._handle_approval_card_action(
                    event=_card_event("ou_peer", "oc_dm"),
                    action_value={"hermes_action": "approve_once", "approval_id": 1},
                    loop=loop,
                )
                self._drain(loop)
        finally:
            loop.close()
        self.assertIsNotNone(response)
        from plugins.platforms.feishu import adapter as feishu_adapter

        if feishu_adapter.CallBackCard is not None:
            self.assertIsNotNone(
                getattr(response, "card", None),
                "authorized DM click must resolve the card inline",
            )
        resolve.assert_called_once_with("agent:main:feishu:dm:oc_dm", "once")

    def test_group_click_without_allowlist_is_dropped(self):
        adapter = _make_adapter(FEISHU_ALLOWED_USERS="")
        loop = asyncio.new_event_loop()
        adapter._loop = loop
        adapter._approval_state[1] = _group_approval_state()
        try:
            with patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve:
                response = adapter._handle_approval_card_action(
                    event=_card_event("ou_member", "oc_grp"),
                    action_value={"hermes_action": "approve_once", "approval_id": 1},
                    loop=loop,
                )
                self._drain(loop)
        finally:
            loop.close()
        self.assertIsNone(getattr(response, "card", None), "unauthorized click must not resolve")
        resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
