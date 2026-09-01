"""Reaction-backed dangerous-command approvals for SimpleX."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import List, Optional

from gateway.platforms.base import SendResult

logger = logging.getLogger(__name__)


class SimplexApprovalMixin:
    async def _set_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji: str,
        *,
        added: bool,
    ) -> SendResult:
        reaction = json.dumps(
            {"type": "emoji", "emoji": emoji}, ensure_ascii=False
        )
        toggle = "on" if added else "off"
        resp = await self._send_command(
            f"/_reaction {self._chat_ref(chat_id)} {message_id} {toggle} {reaction}",
            timeout=10.0,
        )
        return self._send_result_from_response(
            resp, expected={"chatItemReaction"}
        )

    @staticmethod
    def _approval_timeout() -> float:
        try:
            from tools.approval import _get_approval_timeout

            return max(1.0, float(_get_approval_timeout()))
        except Exception:
            return 300.0

    def _approval_text(
        self,
        command: str,
        description: str,
        *,
        allow_session: bool,
        allow_permanent: bool,
        smart_denied: bool,
        reactions: bool,
    ) -> str:
        prefix = self.typed_command_prefix
        lines = [self._format_exec_approval(command, description, smart_denied)]
        choices = [f"Reply `{prefix}approve` to run once"]
        if not smart_denied and allow_session:
            choices.append(f"`{prefix}approve session` for this session")
        if not smart_denied and allow_permanent:
            choices.append(f"`{prefix}approve always` to persist the pattern")
        choices.append(f"`{prefix}deny` to cancel")
        lines.append(", or ".join(choices) + ".")
        if reactions:
            taps = ["✅ = run once"]
            if not smart_denied and allow_session:
                taps.append("🚀 = allow for this session")
            taps.append("👎 = deny")
            lines.append("Tap a reaction: " + "; ".join(taps) + ".")
        return "\n\n".join(lines)

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[dict] = None,
        allow_permanent: bool = True,
        allow_session: bool = True,
        smart_denied: bool = False,
    ) -> SendResult:
        """Offer unambiguous direct-message reaction approvals with typed fallback."""
        now = time.monotonic()
        self._sweep_approval_prompts(now)
        is_dm = not str(chat_id).startswith("group:")
        existing_id = self._approval_prompt_by_session.get(session_key)
        superseded_prior = bool(existing_id)
        typed_only = self._approval_typed_only_until.get(session_key, 0.0) > now

        if existing_id:
            self._retire_approval_prompt(existing_id)
            self._approval_typed_only_until[session_key] = now + self._approval_timeout()
            typed_only = True

        reaction_lane = is_dm and not typed_only
        text = self._approval_text(
            command,
            description,
            allow_session=allow_session,
            allow_permanent=allow_permanent,
            smart_denied=smart_denied,
            reactions=reaction_lane,
        )
        if superseded_prior:
            text = (
                "The earlier approval prompt was superseded; its reactions no "
                "longer apply. Use the typed choices below.\n\n" + text
            )
        result = await self.send(chat_id, text, metadata=metadata)
        if not result.success or not result.message_id or not reaction_lane:
            if not result.success or not result.message_id:
                self._approval_typed_only_until[session_key] = (
                    now + self._approval_timeout()
                )
            return result

        choices = {"✅": "once", "👎": "deny"}
        seeds = ["👎", "✅"]
        if not smart_denied and allow_session:
            choices["🚀"] = "session"
            seeds.insert(1, "🚀")
        prompt = {
            "session_key": session_key,
            "chat_id": str(chat_id),
            "item_id": str(result.message_id),
            "choices": choices,
            "seeded": [],
            "expires_at": now + self._approval_timeout(),
        }
        self._approval_prompts_by_item[prompt["item_id"]] = prompt
        self._approval_prompt_by_session[session_key] = prompt["item_id"]
        self._spawn_command_task(self._seed_approval_reactions(prompt, seeds))
        self._spawn_command_task(self._expire_approval_prompt(prompt["item_id"]))
        return result

    async def _seed_approval_reactions(self, prompt: dict, seeds: List[str]) -> None:
        for emoji in seeds[:3]:
            if self._approval_prompts_by_item.get(prompt["item_id"]) is not prompt:
                return
            result = await self._set_reaction(
                prompt["chat_id"], prompt["item_id"], emoji, added=True
            )
            if not result.success:
                logger.info(
                    "SimpleX: reaction seed unavailable; typed approval remains active"
                )
                return
            prompt["seeded"].append(emoji)

    async def _expire_approval_prompt(self, item_id: str) -> None:
        prompt = self._approval_prompts_by_item.get(item_id)
        if not prompt:
            return
        await asyncio.sleep(max(0.0, prompt["expires_at"] - time.monotonic()))
        if self._approval_prompts_by_item.get(item_id) is prompt:
            self._retire_approval_prompt(item_id)

    def _sweep_approval_prompts(self, now: Optional[float] = None) -> None:
        current = time.monotonic() if now is None else now
        for item_id, prompt in list(self._approval_prompts_by_item.items()):
            if prompt["expires_at"] <= current:
                self._retire_approval_prompt(item_id)
        for session_key, expiry in list(self._approval_typed_only_until.items()):
            if expiry <= current:
                self._approval_typed_only_until.pop(session_key, None)

    def _retire_approval_prompt(self, item_id: str) -> None:
        prompt = self._approval_prompts_by_item.pop(str(item_id), None)
        if not prompt:
            return
        if self._approval_prompt_by_session.get(prompt["session_key"]) == str(item_id):
            self._approval_prompt_by_session.pop(prompt["session_key"], None)
        if prompt["seeded"]:
            self._spawn_command_task(self._clear_approval_reactions(prompt))

    async def _clear_approval_reactions(self, prompt: dict) -> None:
        for emoji in list(prompt["seeded"]):
            await self._set_reaction(
                prompt["chat_id"], prompt["item_id"], emoji, added=False
            )

    @staticmethod
    def _reaction_context(resp: dict) -> Optional[dict]:
        wrapper = resp.get("reaction", {}) or {}
        if not isinstance(wrapper, dict):
            return None
        chat_info = wrapper.get("chatInfo", {}) or {}
        reaction = wrapper.get("chatReaction", {}) or {}
        if not isinstance(chat_info, dict) or not isinstance(reaction, dict):
            return None
        chat_item = reaction.get("chatItem", {}) or {}
        meta = chat_item.get("meta", {}) if isinstance(chat_item, dict) else {}
        item_id = meta.get("itemId") if isinstance(meta, dict) else None
        msg_reaction = reaction.get("reaction", {}) or {}
        emoji = (
            msg_reaction.get("emoji", "")
            if isinstance(msg_reaction, dict)
            else ""
        ).replace("\ufe0f", "")
        chat_type = chat_info.get("type", "")
        chat_dir = reaction.get("chatDir", {}) or {}
        if chat_type == "direct":
            if not isinstance(chat_dir, dict) or chat_dir.get("type") != "directRcv":
                return None
            contact = chat_info.get("contact", {}) or {}
            chat_id = str(contact.get("contactId", ""))
            user_id = chat_id
            user_name = contact.get("localDisplayName", "")
        elif chat_type == "group":
            if not isinstance(chat_dir, dict) or chat_dir.get("type") != "groupRcv":
                return None
            group = chat_info.get("groupInfo", {}) or {}
            chat_id = f"group:{group.get('groupId', '')}"
            member = chat_dir.get("groupMember", {}) if isinstance(chat_dir, dict) else {}
            member_identity = member.get("memberContactId")
            if member_identity is None:
                member_identity = member.get("memberId", "")
            user_id = str(member_identity)
            user_name = member.get("localDisplayName", "")
        else:
            return None
        return {
            "item_id": str(item_id) if item_id is not None else "",
            "chat_id": chat_id,
            "user_id": user_id,
            "user_name": user_name,
            "emoji": emoji,
            "added": bool(resp.get("added", False)),
            "raw": resp,
        }

    async def _handle_reaction_event(self, resp: dict) -> None:
        ctx = self._reaction_context(resp)
        if not ctx:
            return
        hook = getattr(self, "_reaction_handler", None)
        if hook is not None:
            await hook(
                {
                    "event_name": (
                        "reaction:added" if ctx["added"] else "reaction:removed"
                    ),
                    "platform": "simplex",
                    **ctx,
                }
            )

        if not ctx["added"]:
            return
        prompt = self._approval_prompts_by_item.get(ctx["item_id"])
        if not prompt or prompt["chat_id"] != ctx["chat_id"]:
            return
        if not prompt["chat_id"].startswith("group:") and ctx["user_id"] != prompt["chat_id"]:
            logger.warning("SimpleX: ignored approval reaction from another contact")
            return
        if time.monotonic() >= prompt["expires_at"]:
            self._retire_approval_prompt(prompt["item_id"])
            return
        choice = prompt["choices"].get(ctx["emoji"])
        if not choice:
            return

        try:
            from tools.approval import resolve_gateway_approval

            count = int(resolve_gateway_approval(prompt["session_key"], choice) or 0)
        except Exception:
            logger.exception("SimpleX: failed to resolve reaction approval")
            return
        self._retire_approval_prompt(prompt["item_id"])
        acknowledgement = {
            "once": "Approved — running this once.",
            "session": "Approved for this session.",
            "deny": "Denied — the command will not run.",
        }[choice]
        if count <= 0:
            acknowledgement = "That approval is no longer pending. Nothing ran."
        self._spawn_command_task(self.send(prompt["chat_id"], acknowledgement))
