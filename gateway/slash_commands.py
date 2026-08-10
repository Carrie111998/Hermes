"""Gateway slash-command handlers for GatewayRunner.

Extracted from ``gateway/run.py`` (god-file decomposition Phase 3b). These are
the in-session slash commands (/model, /reset, /usage, /compress, ...) the
gateway dispatches from ``_handle_message``. There are 42 of them (~3,200 LOC);
lifting them into a mixin that ``GatewayRunner`` inherits keeps every
``self._handle_*_command`` dispatch + test reference working via the MRO, while
removing the bulk from run.py.

Module-level run.py helpers a handler needs (``_hermes_home``,
``_load_gateway_config``, ``_resolve_gateway_model``, etc.) are imported lazily
inside the handler body â€” a deferred ``from gateway.run import ...`` resolves at
call time (run.py fully loaded by then), avoiding an import cycle.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
import logging
import os
import re
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from agent.account_usage import fetch_account_usage, render_account_usage_lines
from agent.i18n import t
from agent.turn_context import extract_api_content_sidecar
from gateway.config import HomeChannel, Platform, PlatformConfig, persist_home_channel
from gateway.platforms.base import EphemeralReply, MessageEvent, MessageType
from gateway.session import (
    AsyncSessionStore,
    SessionSource,
    build_session_key,
    is_shared_multi_user_session,
)
from hermes_cli.config import atomic_config_write, cfg_get, clear_model_endpoint_credentials
from utils import (
    atomic_json_write,
    base_url_host_matches,
    is_truthy_value,
)

logger = logging.getLogger("gateway.run")

# Upper bound on the off-loop agent-resource cleanup during a /new or /reset
# (see _handle_reset_command). A stuck teardown must not block the event loop;
# past this the reset proceeds and the cleanup is left to finish (or leak) in
# its worker thread. (#35994)
_RESET_CLEANUP_TIMEOUT_S = 30.0


def _clean_str(value: Any) -> str:
    """Strip and return a non-empty string value, or empty string."""
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _int_value(value: Any) -> int:
    """Safely coerce to int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _model_switch_skew_guard() -> Optional[str]:
    """Refuse a model switch when the gateway is running stale code.

    A long-lived gateway holds its modules in memory from boot. If the checkout
    changed underneath it (e.g. a manual ``git pull``), switching models can hit
    a first-time lazy import on a new code path and crash on a stale cached
    dependency â€” the cryptic ``cannot import name 'env_float' from 'utils'``.
    Detect the drift and tell the user to restart instead.

    Intentionally scoped to model switching â€” the known, highest-risk trigger.
    Any first-time lazy import on a stale process is technically exposed; we
    don't guard every import site, only this one.
    """
    from gateway.code_skew import detect_code_skew

    skew = detect_code_skew()
    if not skew:
        return None
    boot_rev, disk_rev = skew
    return t(
        "gateway.model.error_prefix",
        error=(
            f"This gateway is running code from {boot_rev} but the checkout on "
            f"disk is now {disk_rev}. Switching models would risk a stale-module "
            f"crash â€” restart the gateway to load the new code: hermes gateway restart"
        ),
    )


class GatewaySlashCommandsMixin:
    """In-session slash-command handlers for GatewayRunner."""

    async_session_store: AsyncSessionStore

    def _typed_command_prefix_for(self, platform) -> str:
        """Return the prefix users can always type to reach Hermes commands.

        Reads the adapter's ``typed_command_prefix`` capability flag
        (default "/"). Slack and Matrix return "!" because typed "/"
        commands are blocked in Slack threads / reserved by Matrix clients;
        their adapters rewrite "!command" to "/command" on receive.
        Instruction text built for those platforms must show the prefix
        that actually works when typed.
        """
        adapter = self.adapters.get(platform) if getattr(self, "adapters", None) else None
        return getattr(adapter, "typed_command_prefix", "/") if adapter is not None else "/"

    async def _handle_reset_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:
        """Handle /new or /reset command."""
        source = event.source
        
        # Get existing session key
        session_key = self._session_key_for_source(source)
        self._invalidate_session_run_generation(session_key, reason="session_reset")
        # Evict the running-agent slot now that the generation is bumped. The
        # in-flight run's own guarded release (run_generation=old) will return
        # False and leave its dead agent behind; clearing here keeps the slot
        # from becoming a zombie that silently drops all later messages (#28686).
        # Idempotent, so the run's finally calling it again is harmless.
        self._release_running_agent_state(session_key)

        # Snapshot the old entry so on_session_finalize can report the
        # expiring session id before reset_session() rotates it.
        old_entry = self.session_store._entries.get(session_key)

        # Close tool resources on the old agent (terminal sandboxes, browser
        # daemons, background processes) before evicting from cache.
        # Guard with getattr because test fixtures may skip __init__.
        #
        # _cleanup_agent_resources is synchronous and can block for a long time
        # (agent.close() does subprocess teardown; shutdown_memory_provider()
        # may do network IO). This handler runs ON the event loop when a
        # Telegram/Discord/Slack confirm-button click resolves the slash-confirm
        # (see _request_slash_confirm), so an inline call wedges the whole loop
        # and the bot goes silent until restart (#35994). Offload it to a worker
        # thread (via the contextvar-preserving executor helper) with a bounded
        # timeout so the loop is never blocked.
        _cache_lock = getattr(self, "_agent_cache_lock", None)
        if _cache_lock is not None:
            with _cache_lock:
                _cached = self._agent_cache.get(session_key)
                _old_agent = _cached[0] if isinstance(_cached, tuple) else _cached if _cached else None
            if _old_agent is not None:
                try:
                    await asyncio.wait_for(
                        self._run_in_executor_with_context(
                            self._cleanup_agent_resources, _old_agent
                        ),
                        timeout=_RESET_CLEANUP_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    # wait_for cancels the await, but the worker thread cannot be
                    # cancelled â€” a wedged teardown keeps running (or leaks) for
                    # the gateway's lifetime. The reset proceeds regardless.
                    logger.warning(
                        "Agent resource cleanup for session %s exceeded %ss during "
                        "/new reset; proceeding with reset (the worker thread is left "
                        "to finish on its own). (#35994)",
                        session_key, _RESET_CLEANUP_TIMEOUT_S,
                    )
                except Exception as cleanup_exc:
                    logger.warning(
                        "Agent resource cleanup for session %s failed during /new "
                        "reset: %s (#35994)",
                        session_key, cleanup_exc,
                    )
        self._evict_cached_agent(session_key)

        # Conversation boundary: clear ALL conversation-scoped per-session
        # state (model/reasoning overrides, one-turn restores, model notes,
        # last-resolved cache, /queue overflow) + security state in one
        # funnel call. See _CONVERSATION_SCOPED_STATE in gateway/run.py.
        self._clear_conversation_scope(session_key, reason="session_reset")

        # The old conversation's in-flight async delegations end WITH it
        # (#55578): after the reset rotates the session id, their completions
        # would have no live owner â€” a dangling subagent can only burn tokens
        # and park an orphaned payload on the shared queue. Interrupt by the
        # expiring durable session id (delegations dispatched from gateway
        # chats are pinned to it via parent_session_id) and by the routing
        # key as a fallback for older records.
        try:
            from tools.async_delegation import interrupt_for_session

            interrupt_for_session(
                session_key=session_key,
                parent_session_id=str(getattr(old_entry, "session_id", "") or ""),
                reason="session_reset",
            )
        except Exception:
            pass

        try:
            from tools.env_passthrough import clear_env_passthrough
            clear_env_passthrough()
        except Exception:
            pass

        try:
            from tools.credential_files import clear_credential_files
            clear_credential_files()
        except Exception:
            pass

        # Reset the session
        new_entry = await self.async_session_store.reset_session(session_key)

        # (Conversation-scoped overrides + security state were already
        # cleared via _clear_conversation_scope above.)

        _old_sid = old_entry.session_id if old_entry else None

        # Fire plugin on_session_finalize hook (session boundary)
        try:
            from hermes_cli.lifecycle import finalize_session
            finalize_session(
                session_id=_old_sid,
                platform=source.platform.value if source.platform else "",
                reason="new_session",
                old_session_id=_old_sid,
                new_session_id=new_entry.session_id if new_entry else None,
            )
        except Exception:
            pass

        # Emit session:end hook (session is ending)
        await self.hooks.emit("session:end", {
            "platform": source.platform.value if source.platform else "",
            "user_id": source.user_id,
            "session_key": session_key,
        })

        # Emit session:reset hook
        await self.hooks.emit("session:reset", {
            "platform": source.platform.value if source.platform else "",
            "user_id": source.user_id,
            "session_key": session_key,
        })

        # Resolve session config info to surface to the user, scoped to the
        # profile serving this source so a multiplexed /reset //new banner
        # reports the profile's model, not the base config's (#59003).
        try:
            session_info = await asyncio.to_thread(
                self._reset_notice_session_info, source
            )
        except Exception:
            session_info = ""

        if new_entry:
            header = await asyncio.to_thread(self._telegram_topic_new_header, source) or t("gateway.reset.header_default")
        else:
            # No existing session, just create one
            new_entry = await self.async_session_store.get_or_create_session(source, force_new=True)
            header = await asyncio.to_thread(self._telegram_topic_new_header, source) or t("gateway.reset.header_new")

        # Set session title if provided with /new <title>
        _title_arg = event.get_command_args().strip()
        _title_note = ""
        if _title_arg and self._session_db and new_entry:
            from hermes_state import SessionDB
            try:
                sanitized = SessionDB.sanitize_title(_title_arg)
            except ValueError as e:
                sanitized = None
                _title_note = t("gateway.reset.title_rejected", error=str(e))
            if sanitized:
                try:
                    await self._session_db.set_session_title(new_entry.session_id, sanitized)
                    header = t("gateway.reset.header_titled", title=sanitized)
                except ValueError as e:
                    _title_note = t("gateway.reset.title_error_untitled", error=str(e))
                except Exception:
                    pass
            elif not _title_note:
                # sanitize_title returned empty (whitespace-only / unprintable)
                _title_note = t("gateway.reset.title_empty_untitled")
        header = header + _title_note

        # When /new runs inside a Telegram DM topic lane, rewrite the
        # (chat_id, thread_id) â†’ session_id binding so the next message
        # uses the freshly-created session. Without this, the binding
        # still points at the old session and the binding-lookup at the
        # top of _handle_message_with_agent would switch right back.
        if await asyncio.to_thread(self._is_telegram_topic_lane, source) and new_entry is not None:
            try:
                await asyncio.to_thread(self._record_telegram_topic_binding, source, new_entry)
            except Exception:
                logger.debug("Failed to rebind Telegram topic after /new", exc_info=True)

        # Fire plugin on_session_reset hook (new session guaranteed to exist)
        try:
            from hermes_cli.lifecycle import invoke_hook as _invoke_hook
            _new_sid = new_entry.session_id if new_entry else None
            _invoke_hook(
                "on_session_reset",
                session_id=_new_sid,
                platform=source.platform.value if source.platform else "",
                reason="new_session",
                old_session_id=_old_sid,
                new_session_id=_new_sid,
            )
        except Exception:
            pass

        # Append a random tip to the reset message
        try:
            from hermes_cli.tips import get_random_tip
            _tip_line = t("gateway.reset.tip", tip=get_random_tip())
        except Exception:
            _tip_line = ""

        if session_info:
            return EphemeralReply(f"{header}\n\n{session_info}{_tip_line}")
        return EphemeralReply(f"{header}{_tip_line}")

    async def _handle_profile_command(self, event: MessageEvent) -> str:
        """Handle /profile â€” show the profile serving this source and its home.

        On a multiplexed gateway the process-level active profile is always
        the multiplexer's own (usually ``default``), so reporting it would
        answer "defauó~½ŞÚ$z{-®éÜj×ÆUö&÷fUö6öÖÖæB‡6VÆbÂWfVçC¢ÖW76vTWfVçB’Óâ÷F–öæÅ·7G%Ó Ğ¢""$†æFÆRö&÷fR6öÖÖæB(	BVæ&Æö6²v—F–ærvVçBF‡&VB‡2’àĞ Ğ¢F†RvVçBF‡&VB‡2’&R&Æö6¶VB–ç6–FRFööÇ2ö&÷fÂç’v—F–ærf÷ Ğ¢F†RW6W"Fò&W7öæBâF†—2†æFÆW"6–væÇ2F†RWfVçB6òF†RvVç@Ğ¢&W7VÖW2æBF†RFW&Ö–æÅ÷FööÂW†V7WFW2F†R6öÖÖæB–æÆ–æR(	BF†R6ÖPĞ¢fÆ÷r2F†R4Ä’w27–æ6‡&öæ÷W2–çWB‚’&÷fÂàĞ Ğ¢7W÷'G2×VÇF—ÆR6öæ7W'&VçB&÷fÇ2‡&ÆÆVÂ7V&vVçG2ÀĞ¢W†V7WFUö6öFR’âö&÷fV&W6öÇfW2F†RöÆFW7BVæF–ær6öÖÖæC°Ğ¢ö&÷fRÆÆ&W6öÇfW2WfW'’VæF–ær6öÖÖæBBöæ6RàĞ Ğ¢W6vS Ğ¢ö&÷fR(	B&÷fRöÆFW7BVæF–ær6öÖÖæBöæ6PĞ¢ö&÷fRÆÂ(	B&÷fRÄÂVæF–ær6öÖÖæG2Böæ6PĞ¢ö&÷fR6W76–öâ(	B&÷fRöÆFW7B²&VÖVÖ&W"f÷"6W76–öàĞ¢ö&÷fRÆÂ6W76–öâ(	B&÷fRÆÂ²&VÖVÖ&W"f÷"6W76–öàĞ¢ö&÷fRÇv—2(	B&÷fRöÆFW7B²&VÖVÖ&W"W&ÖæVçFÇĞ¢ö&÷fRÆÂÇv—2(	B&÷fRÆÂ²&VÖVÖ&W"W&ÖæVçFÇĞ¢"" Ğ¢6÷W&6RÒWfVçBç6÷W&6PĞ¢6W76–öåö¶W’Ò6VÆbå÷6W76–öåö¶W•öf÷%÷6÷W&6R‡6÷W&6RĞ Ğ¢g&öÒFööÇ2æ&÷fÂ–×÷'B€Ğ¢&W6öÇfUövFWv•ö&÷fÂÂ†5ö&Æö6¶–æuö&÷fÂÀĞ¢Ğ Ğ¢–bæ÷B†5ö&Æö6¶–æuö&÷fÂ‡6W76–öåö¶W’“ Ğ¢–b6W76–öåö¶W’–â6VÆbå÷VæF–æuö&÷fÇ3 Ğ¢6VÆbå÷VæF–æuö&÷fÇ2ç÷‡6W76–öåö¶W’Ğ¢&WGW&âB‚&vFWv’æ&÷fÅöW‡—&VB"Ğ¢&WGW&âB‚&vFWv’æ&÷fRææõ÷VæF–ær"Ğ Ğ¢2'6R&w3¢7W÷'B&ÆÂ"Â&ÆÂ6W76–öâ"Â&ÆÂÇv—2"Â'6W76–öâ"Â&Çv—2 Ğ¢&w2ÒWfVçBævWEö6öÖÖæEö&w2‚’ç7G&—‚’æÆ÷vW"‚’ç7Æ—B‚Ğ¢&W6öÇfUöÆÂÒ&ÆÂ"–â&w0Ğ¢&VÖ–æ–ærÒ¶f÷"–â&w2–bÒ&ÆÂ%ĞĞ Ğ¢–bç’†–â²&Çv—2"Â'W&ÖæVçB"Â'W&ÖæVçFÇ’'Òf÷"–â&VÖ–æ–ær“ Ğ¢6†ö–6RÒ&Çv—2 Ğ¢VÆ–bç’†–â²'6W76–öâ"Â'6W2'Òf÷"–â&VÖ–æ–ær“ Ğ¢6†ö–6RÒ'6W76–öâ Ğ¢VÇ6S Ğ¢6†ö–6RÒ&öæ6R Ğ Ğ¢6÷VçBÒ&W6öÇfUövFWv•ö&÷fÂ‡6W76–öåö¶W’Â6†ö–6RÂ&W6öÇfUöÆÃ×&W6öÇfUöÆÂĞ¢–bæ÷B6÷VçC Ğ¢&WGW&âB‚&vFWv’æ&÷fRææõ÷VæF–ær"Ğ Ğ¢2&W7VÖRG—–ær–æF–6F÷"(	BvVçB—2&÷WBFò6öçF–çVR&ö6W76–æràĞ¢öFFW"Ò6VÆbæFFW'2ævWB‡6÷W&6RçÆFf÷&ÒĞ¢–böFFW# Ğ¢öFFW"ç&W7VÖU÷G—–æuöf÷%ö6†B‡6÷W&6Ræ6†Eö–BĞ Ğ¢ÆövvW"æ–æfò‚%W6W"&÷fVBVBFævW&÷W26öÖÖæB‡2’f–ö&÷fR‚W2’"Â6÷VçBÂ6†ö–6RĞ¢ÇW&ÂÒ'ÇW&Â"–b6÷VçBâVÇ6R'6–æwVÆ" Ğ¢&WGW&âB†b&vFWv’æ&÷fRç¶6†ö–6WÕ÷·ÇW&ÇÒ"Â6÷VçCÖ6÷VçBĞ Ğ¢7–æ2FVbö†æFÆUöFVç•ö6öÖÖæB‡6VÆbÂWfVçC¢ÖW76vTWfVçB’Óâ7G# Ğ¢""$†æFÆRöFVç’6öÖÖæB(	B&V¦V7BVæF–ærFævW&÷W26öÖÖæB‡2’àĞ Ğ¢6–væÇ2&Æö6¶VBvVçBF‡&VB‡2’v—F‚vFVç’r&W7VÇB6òF†W’&V6V—fPĞ¢FVf–æ—F—fR$Äô4´TBÖW76vRÂ6ÖR2F†R4Ä’FVç’fÆ÷ràĞ Ğ¢öFVç–FVæ–W2F†RöÆFW7C²öFVç’ÆÆFVæ–W2WfW'—F†–æràĞ¢öFVç’Ç&V6öãæ†÷"öFVç’ÆÂÇ&V6öãæ’GF6†W2öæRÖÆ–æPĞ¢&V6öâF†B—2&VÆ–VB&6²FòF†RvVçB6ò—B6âFB–ç7FVBö`Ğ¢öæÇ’†V&–ær&FVæ–VB"â÷'FVBg&öÒv–&—F’öææö6Ær3#ƒ3"àĞ¢"" Ğ¢6÷W&6RÒWfVçBç6÷W&6PĞ¢6W76–öåö¶W’Ò6VÆbå÷6W76–öåö¶W•öf÷%÷6÷W&6R‡6÷W&6RĞ Ğ¢g&öÒFööÇ2æ&÷fÂ–×÷'B€Ğ¢&W6öÇfUövFWv•ö&÷fÂÂ†5ö&Æö6¶–æuö&÷fÂÀĞ¢Ğ Ğ¢–bæ÷B†5ö&Æö6¶–æuö&÷fÂ‡6W76–öåö¶W’“ Ğ¢–b6W76–öåö¶W’–â6VÆbå÷VæF–æuö&÷fÇ3 Ğ¢6VÆbå÷VæF–æuö&÷fÇ2ç÷‡6W76–öåö¶W’Ğ¢&WGW&âB‚&vFWv’æFVç’ç7FÆR"Ğ¢&WGW&âB‚&vFWv’æFVç’ææõ÷VæF–ær"Ğ Ğ¢2'6R&w3¢ÆVF–ær&ÆÂ"Fö¶VâFVæ–W2WfW'’VæF–ær6öÖÖæC°Ğ¢2ç—F†–ærgFW"—B†÷"F†Rv†öÆR&r7G&–ærv†Vâ&ÆÂ"—2'6VçB’—0Ğ¢26GW&VBfW&&F–Ò2F†R÷F–öæÂFVç’&V6öâ&VÆ–VBFòF†RvVçBàĞ¢&uö&w2ÒWfVçBævWEö6öÖÖæEö&w2‚’ç7G&—‚Ğ¢Fö¶Vç2Ò&uö&w2ç7Æ—B‚Ğ¢&W6öÇfUöÆÂÒ&ööÂ‡Fö¶Vç2’æBFö¶Vç5³ÒæÆ÷vW"‚’ÓÒ&ÆÂ Ğ¢–b&W6öÇfUöÆÃ Ğ¢&V6öâÒ&uö&w5¶ÆVâ‡Fö¶Vç5³Ò“¥Òç7G&—‚Ğ¢VÇ6S Ğ¢&V6öâÒ&uö&w0Ğ¢26Fò6æRöæRÖÆ–æW#²F†RvVçBöæÇ’æVVG26†÷'B†–çBàĞ¢–b&V6öã Ğ¢&V6öâÒ&V6öå³£#ƒÒç7G&—‚Ğ Ğ¢6÷VçBÒ&W6öÇfUövFWv•ö&÷fÂ€Ğ¢6W76–öåö¶W’Â&FVç’"Â&W6öÇfUöÆÃ×&W6öÇfUöÆÂÀĞ¢&V6öã×&V6öâ÷"æöæRÀĞ¢Ğ¢–bæ÷B6÷VçC Ğ¢&WGW&âB‚&vFWv’æFVç’ææõ÷VæF–ær"Ğ Ğ¢2&W7VÖRG—–ær–æF–6F÷"(	BvVçB6öçF–çVW2‡v—F‚$Äô4´TB&W7VÇB’àĞ¢öFFW"Ò6VÆbæFFW'2ævWB‡6÷W&6RçÆFf÷&ÒĞ¢–böFFW# Ğ¢öFFW"ç&W7VÖU÷G—–æuöf÷%ö6†B‡6÷W&6Ræ6†Eö–BĞ Ğ¢ÆövvW"æ–æfò€Ğ¢%W6W"FVæ–VBVBFævW&÷W26öÖÖæB‡2’f–öFVç’W2"ÀĞ¢6÷VçBÂ"‡v—F‚&V6öâ’"–b&V6öâVÇ6R""ÀĞ¢Ğ¢–b&V6öã Ğ¢–b6÷VçBâ Ğ¢&WGW&âB‚&vFWv’æFVç’æFVæ–VE÷&V6öå÷ÇW&Â"Â6÷VçCÖ6÷VçBÂ&V6öã×&V6öâĞ¢&WGW&âB‚&vFWv’æFVç’æFVæ–VE÷&V6öå÷6–æwVÆ""Â&V6öã×&V6öâĞ¢–b6÷VçBâ Ğ¢&WGW&âB‚&vFWv’æFVç’æFVæ–VE÷ÇW&Â"Â6÷VçCÖ6÷VçBĞ¢&WGW&âB‚&vFWv’æFVç’æFVæ–VE÷6–æwVÆ""Ğ Ğ¢7–æ2FVbö†æFÆUöFV'Vuö6öÖÖæB‡6VÆbÂWfVçC¢ÖW76vTWfVçB’Óâ7G# Ğ¢""$†æFÆRöFV'Vr(	BWÆöBFV'Vr&W÷'B‡7VÖÖ'’öæÇ’’æB&WGW&â7FRU$Ç2àĞ Ğ¢vFWv’WÆöG2ôäÅ’F†R7VÖÖ'’&W÷'B‡7—7FVÒ–æfò²ÆörF–Ç2’ÀĞ¢äõBgVÆÂÆörf–ÆW2ÂFò&÷FV7B6öçfW'6F–öâ&—f7’âW6W'2v†òæVV@Ğ¢gVÆÂÆörWÆöG26†÷VÆBW6R†W&ÖW2FV'Vr6†&Vg&öÒF†R4Ä’àĞ¢"" Ğ¢–×÷'B7–æ6–ğĞ¢g&öÒ†W&ÖW5ö6Æ’æFV'Vr–×÷'B€Ğ¢ö6GW&UöGV×Â6öÆÆV7EöFV'Vu÷&W÷'BÀĞ¢WÆöE÷Fõ÷7FV&–âÂ÷66†VGVÆUöWFõöFVÆWFRÀĞ¢ôtDUt•õ$•d5•ôäõD”4RÂö&W7EöVff÷'E÷7vVWöW‡—&VE÷7FW2ÀĞ¢Ğ Ğ¢Æö÷Ò7–æ6–òævWE÷'Vææ–æuöÆö÷‚Ğ Ğ¢2'Vâ&Æö6¶–ær’ôò†GV×6GW&RÂÆör&VG2ÂWÆöG2’–âF‡&VBàĞ¢FVbö6öÆÆV7EöæE÷WÆöB‚“ Ğ¢ö&W7EöVff÷'E÷7vVWöW‡—&VE÷7FW2‚Ğ¢GV×÷FW‡BÒö6GW&UöGV×‚Ğ¢&W÷'BÒ6öÆÆV7EöFV'Vu÷&W÷'B†ÆöuöÆ–æW3Ó#ÂGV×÷FW‡CÖGV×÷FW‡BĞ Ğ¢W&Ç2Ò·ĞĞ¢G'“ Ğ¢W&Ç5²%&W÷'B%ÒÒWÆöE÷Fõ÷7FV&–â‡&W÷'BĞ¢W†6WBW†6WF–öâ2W†3 Ğ¢&WGW&âB‚&vFWv’æFV'VrçWÆöEöf–ÆVB"ÂW'&÷#ÖW†2Ğ Ğ¢266†VGVÆRWFòÖFVÆWF–öâgFW"b†÷W'0Ğ¢÷66†VGVÆUöWFõöFVÆWFR†Æ—7B‡W&Ç2çfÇVW2‚’’Ğ Ğ¢Æ–æW2ÒµôtDUt•õ$•d5•ôäõD”4RÂ""ÂB‚&vFWv’æFV'Vræ†VFW""’Â"%ĞĞ¢Æ&VÅ÷v–GF‚ÒÖ‚†ÆVâ†²’f÷"²–âW&Ç2Ğ¢f÷"Æ&VÂÂW&Â–âW&Ç2æ—FV×2‚“ Ğ¢Æ–æW2æVæB†b&¶Æ&VÃ£Ç¶Æ&VÅ÷v–GF‡×Ö·W&ÇÒ"Ğ Ğ¢Æ–æW2æVæB‚""Ğ¢Æ–æW2æVæB‡B‚&vFWv’æFV'VræWFõöFVÆWFR"’Ğ¢Æ–æW2æVæB‡B‚&vFWv’æFV'VrægVÆÅöÆöw5ö†–çB"’Ğ¢Æ–æW2æVæB‡B‚&vFWv’æFV'Vrç6†&Uö†–çB"’Ğ¢&WGW&â%Æâ"æ¦ö–â†Æ–æW2Ğ Ğ¢&WGW&âv—BÆö÷ç'Våö–åöW†V7WF÷"„æöæRÂö6öÆÆV7EöæE÷WÆöBĞ Ğ¢7–æ2FVbö†æFÆU÷WFFUö6öÖÖæB‡6VÆbÂWfVçC¢ÖW76vTWfVçB’Óâ7G# Ğ¢""$†æFÆR÷WFFR6öÖÖæB(	BWFFR†W&ÖW2vVçBFòF†RÆFW7BfW'6–öâàĞ Ğ¢7vç2†W&ÖW2WFFV–âFWF6†VB6W76–öâ‡f–6WG6–F’6ò—@Ğ¢7W'f—fW2F†RvFWv’&W7F'BF†B†W&ÖW2WFFVÖ’G&–vvW"âÖ&¶W Ğ¢f–ÆW2&Rw&—GFVâ6òV—F†W"F†R7W'&VçBvFWv’&ö6W72÷"F†RæW‡BöæPĞ¢6âæ÷F–g’F†RW6W"v†VâF†RWFFRf–æ—6†W2àĞ¢"" Ğ¢g&öÒvFWv’ç'Vâ–×÷'Bö†W&ÖW5ö†öÖRÂ÷&W6öÇfUö†W&ÖW5ö&–àĞ¢–×÷'B§6öàĞ¢–×÷'B6‡WF–ÀĞ¢–×÷'B7V'&ö6W70Ğ¢g&öÒFFWF–ÖR–×÷'BFFWF–ÖPĞ¢g&öÒ†W&ÖW5ö6Æ’æ6öæf–r–×÷'B—5öÖævVBÂf÷&ÖEöÖævVEöÖW76vPĞ Ğ¢2&Æö6²æöâÖÖW76v–ærÆFf÷&×2„’6W'fW"ÂvV&†öö·2Â5Ğ¢ÆFf÷&ÒÒWfVçBç6÷W&6RçÆFf÷&ĞĞ¢öÆÆ÷vVBÒ6VÆbåõUDDUôÄÄõtTEõÄDdõ$Õ0Ğ¢2ÇVv–âÆFf÷&×2v—F‚ÆÆ÷u÷WFFUö6öÖÖæCÕG'VR&RÇ6òÆÆ÷vV@Ğ¢–bÆFf÷&Òæ÷B–âöÆÆ÷vVC Ğ¢G'“ Ğ¢g&öÒvFWv’çÆFf÷&Õ÷&Vv—7G'’–×÷'BÆFf÷&Õ÷&Vv—7G'Ğ¢VçG'’ÒÆFf÷&Õ÷&Vv—7G'’ævWB‡ÆFf÷&ÒçfÇVRĞ¢–bæ÷BVçG'’÷"æ÷BVçG'’æÆÆ÷u÷WFFUö6öÖÖæC Ğ¢&WGW&âB‚&vFWv’çWFFRçÆFf÷&Õöæ÷EöÖW76v–ær"Ğ¢W†6WBW†6WF–öã Ğ¢&WGW&âB‚&vFWv’çWFFRçÆFf÷&Õöæ÷EöÖW76v–ær"Ğ Ğ¢–b—5öÖævVB‚“ Ğ¢&WGW&âb.)Ér¶f÷&ÖEöÖævVEöÖW76vR‚wWFFR†W&ÖW2vVçBr—Ò Ğ Ğ¢&ö¦V7E÷&ö÷BÒF‚…õöf–ÆUõò’ç&VçBç&VçBç&W6öÇfR‚Ğ¢v—EöF—"Ò&ö¦V7E÷&ö÷Bòræv—BpĞ Ğ¢–bæ÷Bv—EöF—"æW†—7G2‚“ Ğ¢&WGW&âB‚&vFWv’çWFFRææ÷Eöv—E÷&Wò"Ğ Ğ¢†W&ÖW5ö6ÖBÒ÷&W6öÇfUö†W&ÖW5ö&–â‚Ğ¢–bæ÷B†W&ÖW5ö6ÖC Ğ¢&WGW&âB‚&vFWv’çWFFRæ†W&ÖW5ö6ÖEöæ÷Eöf÷VæB"Ğ Ğ¢VæF–æu÷F‚Òö†W&ÖW5ö†öÖRò"çWFFU÷VæF–æræ§6öâ Ğ¢÷WGWE÷F‚Òö†W&ÖW5ö†öÖRò"çWFFUö÷WGWBçG‡B Ğ¢W†—Eö6öFU÷F‚Òö†W&ÖW5ö†öÖRò"çWFFUöW†—Eö6öFR Ğ¢6W76–öåö¶W’Ò6VÆbå÷6W76–öåö¶W•öf÷%÷6÷W&6R†WfVçBç6÷W&6RĞ¢VæF–ærÒ°Ğ¢'ÆFf÷&Ò#¢WfVçBç6÷W&6RçÆFf÷&ÒçfÇVRÀĞ¢&6†Eö–B#¢WfVçBç6÷W&6Ræ6†Eö–BÀĞ¢&6†E÷G—R#¢WfVçBç6÷W&6Ræ6†E÷G—RÀĞ¢'W6W%ö–B#¢WfVçBç6÷W&6RçW6W%ö–BÀĞ¢'6W76–öåö¶W’#¢6W76–öåö¶W’ÀĞ¢'F–ÖW7F×#¢FFWF–ÖRææ÷r‚’æ—6öf÷&ÖB‚’ÀĞ¢ĞĞ¢–bWfVçBç6÷W&6RçF‡&VEö–C Ğ¢VæF–æu²'F‡&VEö–B%ÒÒWfVçBç6÷W&6RçF‡&VEö–@Ğ¢–bWfVçBæÖW76vUö–C Ğ¢VæF–æu²&ÖW76vUö–B%ÒÒWfVçBæÖW76vUö–@Ğ¢÷F×÷VæF–ærÒVæF–æu÷F‚çv—F…÷7Vff—‚‚"çF×"Ğ¢÷F×÷VæF–ærçw&—FU÷FW‡B†§6öâæGV×2‡VæF–ær’ÂVæ6öF–æsÒ'WFbÓ‚"Ğ¢÷F×÷VæF–ærç&WÆ6R‡VæF–æu÷F‚Ğ¢W†—Eö6öFU÷F‚çVæÆ–æ²†Ö—76–æuöö³ÕG'VRĞ Ğ¢27vâ†W&ÖW2WFFRÒÖvFWv–FWF6†VB6ò—B7W'f—fW2vFWv’&W7F'BàĞ¢2ÒÖvFWv’Væ&ÆW2f–ÆRÖ&6VB•2f÷"–çFW&7F—fR&ö×G2‡7F6€Ğ¢2&W7F÷&RÂ6öæf–rÖ–w&F–öâ’6òF†RvFWv’6âf÷'v&BF†VÒFòF†PĞ¢2W6W"–ç7FVBöb6–ÆVçFÇ’6¶—–ærF†VÒàĞ¢2W6R6WG6–Bf÷"÷'F&ÆR6W76–öâFWF6‚‡v÷&·2VæFW"7—7FVÒ6W'f–6W0Ğ¢2v†W&R7—7FVÖB×'VâÒ×W6W"f–Ç2GVRFòÖ—76–ærBÔ'W26W76–öâ’àĞ¢2•D„ôåTä%TddU$TBVç7W&W2÷WGWB—2fÇW6†VBÆ–æRÖ'’ÖÆ–æR6òF†PĞ¢2vFWv’6â7G&VÒ—BFòF†RÖW76VævW"–âæV"×&VÂ×F–ÖRàĞ¢27vâ†W&ÖW2WFFRÒÖvFWv–FWF6†VB6ò—B7W'f—fW2vFWv’&W7F'BàĞ¢2ÒÖvFWv’Væ&ÆW2f–ÆRÖ&6VB•2f÷"–çFW&7F—fR&ö×G2‡7F6€Ğ¢2&W7F÷&RÂ6öæf–rÖ–w&F–öâ’6òF†RvFWv’6âf÷'v&BF†VÒFòF†PĞ¢2W6W"–ç7FVBöb6–ÆVçFÇ’6¶—–ærF†VÒàĞ¢2W6R6WG6–Bf÷"÷'F&ÆR6W76–öâFWF6‚‡v÷&·2VæFW"7—7FVÒ6W'f–6W0Ğ¢2v†W&R7—7FVÖB×'VâÒ×W6W"f–Ç2GVRFòÖ—76–ærBÔ'W26W76–öâ’àĞ¢2•D„ôåTä%TddU$TBVç7W&W2÷WGWB—2fÇW6†VBÆ–æRÖ'’ÖÆ–æR6òF†PĞ¢2vFWv’6â7G&VÒ—BFòF†RÖW76VævW"–âæV"×&VÂ×F–ÖRàĞ¢0Ğ¢2v–æF÷w3¢æò&6‚÷6WG6–B6†–ââ'Vâ†W&ÖW2WFFRÒÖvFWv– Ğ¢2F—&V7FÇ’f–7—2æW†V7WF&ÆS²&VF—&V7B7FF÷WB÷7FFW'"FòF†R6ÖPĞ¢2÷WGWBf–ÆW2f–÷Vâf–ÆR†æFÆW3²w&—FRF†RW†—B6öFR–âĞ¢2föÆÆ÷r×Ww&—FRâF–ç’—F†öâvF6†W"v÷VÆB&R6ÆVæW"'W@Ğ¢2vRw&RÇ&VG’–ç6–FRvFWv’÷'Vâç’w2WFFRF‚v†–6‚—27–æ2ÀĞ¢26òF†R6–×ÆW7B6÷'&V7BF†–ær—3¢ÆVæ6‚â–æÆ–æR—F†öâ†VÇW Ğ¢2F†B'Vç2F†R6öÖÖæBæBw&—FW2&÷F‚÷WGWG2àĞ¢G'“ Ğ¢–b7—2çÆFf÷&ÒÓÒ'v–ã3"# Ğ¢–×÷'BFW‡Gw& Ğ¢g&öÒ†W&ÖW5ö6Æ’å÷7V'&ö6W75ö6ö×B–×÷'Bv–æF÷w5öFWF6…÷÷Våö·v&w0Ğ Ğ¢2†W&ÖW5ö6ÖB—2Æ—7Böb&wb'G2vR6â72F—&V7FÇĞ¢2†æò6†VÆÂ×V÷F–æræVVFVB’àĞ¢†VÇW"ÒFW‡Gw&æFVFVçB€Ğ¢"" Ğ¢–×÷'B÷2Â7V'&ö6W72Â7—0Ğ¢÷WGWE÷F‚Ò7—2æ&we³ĞĞ¢W†—Eö6öFU÷F‚Ò7—2æ&we³%ĞĞ¢6ÖBÒ7—2æ&we³3¥ĞĞ¢VçbÒF–7B†÷2æVçf—&öâĞ¢Vçe²%•D„ôåTä%TddU$TB%ÒÒ# Ğ¢v—F‚÷Vâ†÷WGWE÷F‚Â'v""’2c Ğ¢&ö2Ò7V'&ö6W72å÷Vâ†6ÖBÂ7FF÷WCÖbÂ7FFW'#×7V'&ö6W72å5DDõUBÂVçcÖVçbĞ¢&2Ò&ö2çv—B‡F–ÖV÷WCÓ3cĞ¢v—F‚÷Vâ†W†—Eö6öFU÷F‚Â'r"ÂVæ6öF–æsÒ'WFbÓ‚"’2c Ğ¢bçw&—FR‡7G"‡&2’Ğ¢"" Ğ¢’ç7G&—‚Ğ¢7V'&ö6W72å÷Vâ€Ğ¢°Ğ¢7—2æW†V7WF&ÆRÂ"Ö2"Â†VÇW"ÀĞ¢7G"†÷WGWE÷F‚’Â7G"†W†—Eö6öFU÷F‚’ÀĞ¢¦†W&ÖW5ö6ÖBÂ'WFFR"Â"ÒÖvFWv’"ÀĞ¢ÒÀĞ¢7FF÷WC×7V'&ö6W72äDUdåTÄÂÀĞ¢7FFW'#×7V'&ö6W72äDUdåTÄÂÀĞ¢¢§v–æF÷w5öFWF6…÷÷Våö·v&w2‚’ÀĞ¢Ğ¢VÇ6S Ğ¢†W&ÖW5ö6ÖE÷7G"Ò""æ¦ö–â‡6†ÆW‚çV÷FR‡'B’f÷"'B–â†W&ÖW5ö6ÖBĞ¢WFFUö6ÖBÒ€Ğ¢b%•D„ôåTä%TddU$TCÓ¶†W&ÖW5ö6ÖE÷7G'ÒWFFRÒÖvFWv’ Ğ¢b"â·6†ÆW‚çV÷FR‡7G"†÷WGWE÷F‚’—Ò#âc² Ğ¢2fö–B7FGW3ÒCö¢7FGW6—2&VBÖöæÇ’7V6–Â&ÖWFW Ğ¢2–â§6‚ÂæBF†—26öÖÖæB7G&–ær—26÷–VB÷&WW6VB–âÖ4õ2÷§6€Ğ¢2÷W&F÷"w&W'2â¶VWF†RFV×ÆFR§6‚×6fRWfVâF†÷Vv‚F†—0Ğ¢27V6–f–27V'&ö6W727W'&VçFÇ’'Vç2VæFW"&6‚àĞ¢b'&3ÒCó²&–çFbrW2rÂ"G&5Â"â·6†ÆW‚çV÷FR‡7G"†W†—Eö6öFU÷F‚’—Ò Ğ¢Ğ¢6WG6–Eö&–âÒ6‡WF–Âçv†–6‚‚'6WG6–B"Ğ¢–b6WG6–Eö&–ã Ğ¢2&VfW'&VC¢6WG6–B7&VFW2æWr6W76–öâÂgVÆÇ’FWF6†V@Ğ¢7V'&ö6W72å÷Vâ€Ğ¢·6WG6–Eö&–âÂ&&6‚"Â"Ö2"ÂWFFUö6ÖEÒÀĞ¢7FF÷WC×7V'&ö6W72äDUdåTÄÂÀĞ¢7FFW'#×7V'&ö6W72äDUdåTÄÂÀĞ¢7F'EöæWu÷6W76–öãÕG'VRÀĞ¢Ğ¢VÇ6S Ğ¢2fÆÆ&6³¢7F'EöæWu÷6W76–öãÕG'VR6ÆÇ2÷2ç6WG6–B‚’–â6†–Æ@Ğ¢7V'&ö6W72å÷Vâ€Ğ¢²&&6‚"Â"Ö2"ÂWFFUö6ÖEÒÀĞ¢7FF÷WC×7V'&ö6W72äDUdåTÄÂÀĞ¢7FFW'#×7V'&ö6W72äDUdåTÄÂÀĞ¢7F'EöæWu÷6W76–öãÕG'VRÀĞ¢Ğ¢W†6WBW†6WF–öâ2S Ğ¢VæF–æu÷F‚çVæÆ–æ²†Ö—76–æuöö³ÕG'VRĞ¢W†—Eö6öFU÷F‚çVæÆ–æ²†Ö—76–æuöö³ÕG'VRĞ¢&WGW&âB‚&vFWv’çWFFRç7F'Eöf–ÆVB"ÂW'&÷#ÖRĞ Ğ¢6VÆbå÷66†VGVÆU÷WFFUöæ÷F–f–6F–öå÷vF6‚‚Ğ¢&WGW&âB‚&vFWv’çWFFRç7F'F–ær"Ğ